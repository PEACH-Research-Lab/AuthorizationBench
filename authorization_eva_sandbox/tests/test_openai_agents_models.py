"""What a profile puts on the wire through the OpenAI Agents SDK.

A local server records each request body. No provider is called. The process's
installed agents release selects the chat backend: 0.20 exercises LiteLLM,
0.22 exercises native Chat Completions. The test skips when the SDK is absent
so the OpenHands environment can still collect it.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import tomllib
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

try:
    import agents  # noqa: F401
except ImportError:
    agents = None

EXAMPLE = Path(__file__).resolve().parents[1] / "evaluation" / "config.toml.example"
BENCHMARK = ("gpt5_6_sol", "sonnet5", "gemini38_flash", "glm53")
LOCKS = {"sonnet5": "anthropic", "gemini38_flash": "google-ai-studio", "glm53": "z-ai"}
CAP_FIELDS = ("max_tokens", "max_output_tokens", "max_completion_tokens")


class _Recorder(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        _Recorder.bodies.append((self.path, body))
        if self.path.endswith("/responses"):
            out = {
                "id": "resp_1",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": body.get("model"),
                "output": [{
                    "type": "message",
                    "id": "m1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            }
        else:
            out = {
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model"),
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "logprobs": None,
                    "message": {"role": "assistant", "content": "ok"},
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def _agents_version() -> str:
    from importlib.metadata import version

    return version("openai-agents")


def _chat_backend() -> str:
    version = _agents_version()
    if version.startswith("0.20."):
        return "litellm"
    return "native"


def _effort_fields(body: dict) -> list[tuple[str, object]]:
    found = []
    if "reasoning_effort" in body:
        found.append(("reasoning_effort", body["reasoning_effort"]))
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        found.append(("reasoning.effort", reasoning["effort"]))
    return found


def _cap_fields(body: dict) -> dict:
    return {key: body[key] for key in CAP_FIELDS if key in body}


def _cache_marks(messages: list | None) -> list[tuple[int, str, str]]:
    found = []
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if "cache_control" in message:
            found.append((index, role, "message"))
        content = message.get("content")
        if isinstance(content, list):
            for block_index, block in enumerate(content):
                if isinstance(block, dict) and "cache_control" in block:
                    found.append((index, role, f"block:{block_index}"))
    return found


@unittest.skipUnless(agents is not None, "openai-agents is not installed")
class OpenAIAgentsModelWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            disable_tracing,
        )

        disable_tracing()
        cls.backend = _chat_backend()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"
        cls.profiles = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["llm"]
        cls._patches = []
        cls.timeouts: list = []
        cls.litellm_kwargs: list = []
        cls._install_observers()

    @classmethod
    def _install_observers(cls):
        if cls.backend == "litellm":
            import litellm

            original = litellm.acompletion

            async def wrapped(*args, **kwargs):
                cls.litellm_kwargs.append(kwargs)
                cls.timeouts.append(kwargs.get("timeout"))
                return await original(*args, **kwargs)

            litellm.acompletion = wrapped
            cls._patches.append(("litellm", original))
        else:
            import agents.run_internal.model_retry as model_retry

            original = model_retry._await_model_attempt

            async def wrapped(awaitable, timeout, **kwargs):
                cls.timeouts.append(timeout)
                return await original(awaitable, timeout, **kwargs)

            model_retry._await_model_attempt = wrapped
            cls._patches.append(("retry", original))

        try:
            from openai.resources.responses.responses import AsyncResponses
        except ImportError:
            AsyncResponses = None
        if AsyncResponses is not None and cls.backend == "litellm":
            original_create = AsyncResponses.create

            async def create(self, *args, **kwargs):
                cls.timeouts.append(kwargs.get("timeout"))
                return await original_create(self, *args, **kwargs)

            AsyncResponses.create = create
            cls._patches.append(("responses", original_create))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for kind, original in cls._patches:
            if kind == "litellm":
                import litellm

                litellm.acompletion = original
            elif kind == "retry":
                import agents.run_internal.model_retry as model_retry

                model_retry._await_model_attempt = original
            elif kind == "responses":
                from openai.resources.responses.responses import AsyncResponses

                AsyncResponses.create = original

    def _send(self, profile):
        from agents import Agent, Runner

        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            model_for_profile,
            run_config,
        )

        _Recorder.bodies = []
        self.timeouts.clear()
        self.litellm_kwargs.clear()
        wired = {**profile, "api_key": "x", "base_url": self.base}

        async def once():
            model, settings, info = model_for_profile(wired, chat_backend=self.backend)
            agent = Agent(
                name="wire",
                instructions="Reply with ok.",
                model=model,
                model_settings=settings,
            )
            await Runner.run(agent, "hi", max_turns=2, run_config=run_config())
            return info

        info = asyncio.run(once())
        self.assertEqual(len(_Recorder.bodies), 1, _Recorder.bodies)
        path, body = _Recorder.bodies[0]
        self.assertNotIn("trace", path)
        return path, body, info

    def _assert_timeout(self, info):
        self.assertTrue(self.timeouts, "timeout did not reach the request layer")
        self.assertTrue(all(value == 150 or value == 150.0 for value in self.timeouts), self.timeouts)
        if info.api_path == "litellm_chat":
            self.assertEqual(self.litellm_kwargs[0].get("num_retries"), 0)
            self.assertEqual(self.litellm_kwargs[0].get("max_retries"), 0)

    def test_benchmark_profiles_send_one_effort_lock_and_cap(self):
        for name in BENCHMARK:
            for effort in ("low", "high"):
                with self.subTest(profile=f"{name}_{effort}", backend=self.backend):
                    profile = self.profiles[f"{name}_{effort}"]
                    path, body, info = self._send(profile)
                    self._assert_timeout(info)
                    self.assertEqual(info.effort, effort)
                    self.assertEqual(info.max_output_tokens, 40000)
                    fields = _effort_fields(body)
                    self.assertEqual(len(fields), 1, body)
                    self.assertEqual(fields[0][1], effort)
                    caps = _cap_fields(body)
                    self.assertEqual(list(caps.values()), [40000], caps)
                    if name == "gpt5_6_sol":
                        self.assertTrue(path.endswith("/responses"), path)
                        self.assertEqual(info.api_path, "responses")
                        self.assertEqual(body["model"], "gpt-5.6-sol")
                        self.assertEqual(fields[0][0], "reasoning.effort")
                        self.assertNotIn("reasoning_effort", body)
                        self.assertIsNone(info.provider_lock)
                    else:
                        self.assertTrue(path.endswith("/chat/completions"), path)
                        self.assertEqual(fields[0][0], "reasoning_effort")
                        self.assertNotIn("effort", body.get("reasoning") or {})
                        self.assertEqual(body["provider"], {
                            "only": [LOCKS[name]],
                            "allow_fallbacks": False,
                            "require_parameters": True,
                        })
                        self.assertEqual(info.provider_lock, body["provider"])
                        self.assertEqual(body.get("usage"), {"include": True})
                        wire_id = profile["model"].removeprefix("openrouter/")
                        self.assertEqual(body["model"], wire_id)
                        if self.backend == "litellm":
                            self.assertEqual(info.api_path, "litellm_chat")
                            self.assertEqual(info.model_id, profile["model"])
                            marks = _cache_marks(self.litellm_kwargs[-1].get("messages"))
                            if name == "sonnet5":
                                roles = [role for _index, role, _where in marks]
                                self.assertIn("system", roles, marks)
                                self.assertTrue(
                                    any(role in ("user", "tool") for _index, role, _where in marks),
                                    marks,
                                )
                            else:
                                self.assertEqual(marks, [], marks)
                        else:
                            self.assertEqual(info.api_path, "native_chat")
                            self.assertEqual(info.model_id, wire_id)

    def test_openhands_cache_marks_the_system_block_and_the_last_user_or_tool(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            apply_openhands_prompt_cache,
        )

        messages = [
            {"role": "system", "content": [
                {"type": "text", "text": "static"},
                {"type": "text", "text": "dynamic"},
            ]},
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "ok"},
            {"role": "tool", "content": "result", "tool_call_id": "c1"},
        ]
        apply_openhands_prompt_cache(messages)
        self.assertEqual(messages[0]["content"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", messages[0]["content"][1])
        self.assertNotIn("cache_control", messages[1])
        self.assertEqual(messages[3]["cache_control"], {"type": "ephemeral"})

    def test_litellm_usage_reads_cache_read_and_write(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            _usage_from_litellm,
        )

        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=2,
            completion_tokens_details=None,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4, cache_write_tokens=3),
        )
        recorded = _usage_from_litellm(SimpleNamespace(usage=usage))
        self.assertEqual(recorded["cached_tokens"], 4)
        self.assertEqual(recorded["cache_write_tokens"], 3)
        recorded = _usage_from_litellm(SimpleNamespace(usage={
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "cache_read_input_tokens": 6,
            "cache_creation_input_tokens": 5,
        }))
        self.assertEqual(recorded["cached_tokens"], 6)
        self.assertEqual(recorded["cache_write_tokens"], 5)

    def test_gpt4_1_sends_no_effort(self):
        path, body, info = self._send(self.profiles["gpt4_1"])
        self._assert_timeout(info)
        self.assertTrue(path.endswith("/responses"), path)
        self.assertEqual(info.api_path, "responses")
        self.assertEqual(body["model"], "gpt-4.1")
        self.assertEqual(_effort_fields(body), [])
        self.assertIsNone(info.effort)
        self.assertEqual(_cap_fields(body), {})

    def test_legacy_nested_effort_moves_to_one_field(self):
        profile = {
            "model": "openrouter/z-ai/glm-5.3",
            "completion_kwargs": {"extra_body": {
                "provider": {"only": ["z-ai"]},
                "reasoning": {"effort": "low", "exclude": True},
            }},
        }
        _, body, info = self._send(profile)
        self.assertEqual(info.effort, "low")
        fields = _effort_fields(body)
        self.assertEqual(fields, [("reasoning_effort", "low")])
        self.assertEqual(body.get("reasoning"), {"exclude": True})

    def test_conflicting_efforts_are_a_config_error(self):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            model_for_profile,
        )

        profile = {
            "model": "openrouter/z-ai/glm-5.3",
            "reasoning_effort": "high",
            "completion_kwargs": {"extra_body": {"reasoning": {"effort": "low"}}},
        }
        with self.assertRaises(ValueError):
            model_for_profile(profile, chat_backend=self.backend)

    def test_published_tools_on_the_wire_omit_think(self):
        from agents import Agent, Runner

        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            model_for_profile,
            run_config,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
            WorkspaceTools,
            function_tools,
        )

        tools = function_tools(
            ["terminal", "read_file", "write_file", "finish"],
            WorkspaceTools("unused", user="openhands", cwd="/workspace"),
        )
        profile = {**self.profiles["gpt4_1"], "api_key": "x", "base_url": self.base}
        _Recorder.bodies = []

        async def once():
            model, settings, _info = model_for_profile(profile, chat_backend=self.backend)
            agent = Agent(
                name="wire",
                instructions="Reply with ok.",
                tools=tools,
                model=model,
                model_settings=settings,
            )
            await Runner.run(agent, "hi", max_turns=2, run_config=run_config())

        asyncio.run(once())
        self.assertEqual(len(_Recorder.bodies), 1, _Recorder.bodies)
        _path, body = _Recorder.bodies[0]
        names = []
        for tool in body.get("tools") or []:
            function = tool.get("function") if isinstance(tool, dict) else None
            source = function if isinstance(function, dict) else tool
            names.append(source.get("name") if isinstance(source, dict) else None)
        self.assertEqual(names, ["terminal", "read_file", "write_file", "finish"])


if __name__ == "__main__":
    unittest.main()
