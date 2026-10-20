"""Offline acceptance: the adapter's request for all 8 settings.

Uses ``SandboxAgentsRuntime`` against the local recorder. No provider is
called. Checks effort, the 40000 cap, the provider lock, that
``context_management`` is absent, that ``apply_patch`` is a function tool on
the chat path and a custom tool on Responses, and that the sent system text
contains the SDK base prompt and our text once.

    .venv-openai-agents/bin/python -m \
        authorization_eva_sandbox.evaluation.harness.openai_sandbox.probe_accept_wire
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from authorization_eva_sandbox.evaluation.harness.openai_sandbox import spike_common as sc
from authorization_eva_sandbox.evaluation.harness.openai_sandbox.recorder import Recorder
from authorization_eva_sandbox.evaluation.harness.openai_sandbox.runtime import (
    SandboxAgentsRuntime,
)

OUT = sc.CAPTURES / "accept_wire"
OURS = "ACCEPT_OURS\n<PRIOR_MEMORY>\nremember this\n</PRIOR_MEMORY>\n"
LOCKS = {"sonnet5": "anthropic", "gemini38_flash": "google-ai-studio", "glm53": "z-ai"}
CAP_FIELDS = ("max_tokens", "max_output_tokens", "max_completion_tokens")
PROMPT_SHAPED = {"instructions", "input", "messages", "tools"}


def _wired(recorder: Recorder, name: str) -> dict[str, Any]:
    return {**sc.example_profiles()[name], "api_key": "x", "base_url": recorder.base_url}


def _system_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("instructions"), str):
        return body["instructions"]
    parts = []
    for message in body.get("messages") or []:
        if message.get("role") == "system":
            content = message.get("content")
            parts.append(content if isinstance(content, str) else json.dumps(content))
    return "\n".join(parts)


def _tool_names(body: dict[str, Any]) -> list[str]:
    names = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = function.get("name") or tool.get("name")
        names.append(f"{tool.get('type')}:{name}")
    return names


async def main() -> None:
    from agents import Agent, Runner

    from authorization_eva_sandbox.evaluation.harness.agent_container import IMAGE_USER
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
        disable_tracing,
        model_for_profile,
        run_config,
    )
    from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
        WorkspaceTools,
        function_tools,
    )

    disable_tracing()
    OUT.mkdir(parents=True, exist_ok=True)
    tc = sc.TaskContainer(None).start()
    summary: dict[str, Any] = {}
    workspace = WorkspaceTools(tc.container.name, user=IMAGE_USER, cwd="/workspace")
    tools = function_tools(["finish"], workspace)
    try:
        session = await sc.attach_session(tc)
        with Recorder(OUT / "requests.jsonl") as recorder:
            for name in sc.BENCHMARK_PROFILES:
                profile = _wired(recorder, name)
                chat = sc.is_chat_profile(profile)
                model, settings, info = model_for_profile(profile, chat_backend="litellm")
                plain = Agent(
                    name="authorization-bench", instructions=OURS, tools=list(tools),
                    model=model, model_settings=settings,
                    tool_use_behavior={"stop_at_tool_names": ["finish"]},
                )
                recorder.load([{"text": "DONE"}])
                await Runner.run(plain, "reply DONE", max_turns=1, run_config=run_config())
                plain_body = recorder.requests[0]["body"]
                model2, settings2, _ = model_for_profile(profile, chat_backend="litellm")
                runtime = SandboxAgentsRuntime(
                    container_view=object(),
                    chat_path=chat,
                    prompt_condition="native",
                    instruction="reply DONE",
                    system_prompt=OURS,
                    model=model2,
                    task="accept",
                    model_name=str(profile["model"]),
                    model_settings=settings2,
                    local_tools=list(tools),
                )
                runtime.session = session
                agent = runtime._make_agent(
                    model2, settings2, {"stop_at_tool_names": ["finish"]},
                )
                recorder.load([{"text": "DONE"}])
                await Runner.run(
                    agent, "reply DONE", max_turns=1, run_config=runtime._run_config(),
                )
                body = recorder.requests[0]["body"]
                sent = _system_text(body)
                tools_sent = _tool_names(body)
                patch = [item for item in tools_sent if item.endswith(":apply_patch")]
                base = name.rsplit("_", 1)[0]
                effort = name.rsplit("_", 1)[1]
                rest_plain = {k: v for k, v in plain_body.items() if k not in PROMPT_SHAPED}
                rest = {k: v for k, v in body.items() if k not in PROMPT_SHAPED}
                caps = {k: body[k] for k in CAP_FIELDS if k in body}
                summary[name] = {
                    "api_path": info.api_path,
                    "context_management": body.get("context_management"),
                    "apply_patch": patch,
                    "ours_once": sent.count("ACCEPT_OURS") == 1,
                    "prior_memory_once": sent.count("<PRIOR_MEMORY>") == 1,
                    "sdk_base_present": "apply_patch" in sent and "ACCEPT_OURS" in sent,
                    "non_prompt_fields_equal": rest_plain == rest,
                    "diff_keys": sorted(
                        k for k in set(rest_plain) | set(rest) if rest_plain.get(k) != rest.get(k)
                    ),
                    "cap_is_40000": list(caps.values()) == [40000],
                    "effort": effort,
                    "provider_lock_ok": (
                        body.get("provider") == {
                            "only": [LOCKS[base]], "allow_fallbacks": False,
                            "require_parameters": True,
                        }
                        if base in LOCKS else "provider" not in body
                    ),
                }
                print(name, json.dumps(summary[name]), flush=True)
    finally:
        workspace.close()
        tc.close()
    sc.write_json(OUT / "summary.json", summary)


if __name__ == "__main__":
    asyncio.run(main())
