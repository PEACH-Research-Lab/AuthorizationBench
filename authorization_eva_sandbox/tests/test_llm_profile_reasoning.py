"""What a profile puts on the wire: one reasoning effort, the provider lock, the output cap.

A local server records each request body; no provider is called.
"""
import json
import os
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from authorization_eva_sandbox.evaluation.run_eval import (  # noqa: E402
    _resolve_outputs_path,
    effort_from_profile,
    llm_from_profile,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "evaluation" / "config.toml.example"
BENCHMARK = ("gpt5_6_sol", "sonnet5", "gemini38_flash", "glm53")
LOCKS = {"sonnet5": "anthropic", "gemini38_flash": "google-ai-studio", "glm53": "z-ai"}


class _Recorder(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        _Recorder.bodies.append((self.path, body))
        if self.path.endswith("/responses"):
            out = {"id": "resp_1", "object": "response", "created_at": 0, "status": "completed",
                   "model": body.get("model"), "output": [{
                       "type": "message", "id": "m1", "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
                   "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                             "input_tokens_details": {"cached_tokens": 0},
                             "output_tokens_details": {"reasoning_tokens": 0}}}
        else:
            out = {"id": "c1", "object": "chat.completion", "created": 0, "model": body.get("model"),
                   "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "ok"}}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class ProfileWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.profiles = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["llm"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _send(self, profile):
        from openhands.sdk.llm import Message, TextContent
        _Recorder.bodies = []
        llm = llm_from_profile({**profile, "api_key": "x", "base_url": self.base})
        messages = [Message(role="user", content=[TextContent(text="hi")])]
        (llm.responses if llm.uses_responses_api() else llm.completion)(messages=messages)
        self.assertEqual(len(_Recorder.bodies), 1)
        return _Recorder.bodies[0]

    def test_benchmark_profiles_send_one_effort_lock_and_cap(self):
        for name in BENCHMARK:
            for effort in ("low", "high"):
                with self.subTest(profile=f"{name}_{effort}"):
                    path, body = self._send(self.profiles[f"{name}_{effort}"])
                    if name == "gpt5_6_sol":
                        self.assertTrue(path.endswith("/responses"))
                        self.assertEqual(body["reasoning"], {"effort": effort})
                        self.assertNotIn("reasoning_effort", body)
                        self.assertEqual(body["max_output_tokens"], 40000)
                    else:
                        self.assertEqual(body["reasoning_effort"], effort)
                        self.assertNotIn("effort", body.get("reasoning") or {})
                        self.assertEqual(body["max_completion_tokens"], 40000)
                        self.assertEqual(body["provider"], {"only": [LOCKS[name]],
                                                            "allow_fallbacks": False,
                                                            "require_parameters": True})

    def test_legacy_nested_effort_moves_to_the_field(self):
        profile = {"model": "openrouter/z-ai/glm-5.3", "completion_kwargs": {"extra_body": {
            "provider": {"only": ["z-ai"]}, "reasoning": {"effort": "low", "exclude": True}}}}
        _, body = self._send(profile)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["reasoning"], {"exclude": True})

    def test_conflicting_efforts_are_a_config_error(self):
        profile = {"model": "openrouter/z-ai/glm-5.3", "reasoning_effort": "high",
                   "completion_kwargs": {"extra_body": {"reasoning": {"effort": "low"}}}}
        with self.assertRaises(ValueError):
            effort_from_profile(profile)

    def test_non_reasoning_model_sends_no_effort(self):
        _, body = self._send({"model": "gpt-4.1"})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("reasoning", body)

    def test_output_directory_separates_efforts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            low = _resolve_outputs_path(tmp, "T0001-b-e-h", "gpt-5.6-sol", "low")
            high = _resolve_outputs_path(tmp, "T0001-b-e-h", "gpt-5.6-sol", "high")
            self.assertIn("gpt-5.6-sol-low", low)
            self.assertIn("gpt-5.6-sol-high", high)


if __name__ == "__main__":
    unittest.main()
