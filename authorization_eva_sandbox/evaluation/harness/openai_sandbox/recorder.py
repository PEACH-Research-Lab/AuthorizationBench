"""Local model endpoint for the spike: records every request, answers from a script.

Serves ``POST .../responses`` (Responses API) and ``POST .../chat/completions``
(LiteLLM and native Chat Completions). No provider is called. Authorization
headers are dropped before anything is kept.

A script is a list of turns. A turn is ``{"text": "..."}`` or
``{"calls": [{"name": ..., "arguments": {...}}]}``; a call with
``"custom": True`` becomes a Responses ``custom_tool_call`` whose ``input`` is
the raw ``arguments`` string. ``{"dynamic": fn}`` calls ``fn(request_body)``
for the turn. When the script runs out the reply is ``DONE``.
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_DROP_HEADERS = {"authorization", "api-key", "x-api-key", "cookie"}


class Recorder:
    def __init__(self, capture_path: Path | None = None):
        self.requests: list[dict[str, Any]] = []
        self.script: list[dict[str, Any]] = []
        self.capture_path = capture_path
        self._lock = threading.Lock()
        recorder = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                headers = {
                    k: v for k, v in self.headers.items() if k.lower() not in _DROP_HEADERS
                }
                reply = recorder._reply(self.path, body, headers)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                with recorder._lock:
                    recorder.requests.append({"path": self.path, "method": "GET"})
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> "Recorder":
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()

    def load(self, script: list[dict[str, Any]]) -> None:
        with self._lock:
            self.script = list(script)
            self.requests = []

    def _reply(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            record = {"path": path, "method": "POST", "headers": headers, "body": body}
            self.requests.append(record)
            if self.capture_path is not None:
                self.capture_path.parent.mkdir(parents=True, exist_ok=True)
                with self.capture_path.open("a") as handle:
                    handle.write(json.dumps(record, default=str) + "\n")
            turn = self.script.pop(0) if self.script else {"text": "DONE"}
        if "dynamic" in turn:
            turn = turn["dynamic"](body)
        if path.endswith("/responses"):
            return _responses_reply(body, turn)
        return _chat_reply(body, turn)


def _arguments(call: dict[str, Any]) -> str:
    args = call.get("arguments", {})
    return args if isinstance(args, str) else json.dumps(args)


def _responses_reply(body: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    for call in turn.get("calls") or []:
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        if call.get("custom"):
            output.append({
                "type": "custom_tool_call",
                "id": f"ctc_{uuid.uuid4().hex[:8]}",
                "call_id": call_id,
                "name": call["name"],
                "input": _arguments(call),
                "status": "completed",
            })
        else:
            output.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:8]}",
                "call_id": call_id,
                "name": call["name"],
                "arguments": _arguments(call),
                "status": "completed",
            })
    if not output:
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": turn.get("text", "DONE"), "annotations": []}],
        })
    return {
        "id": f"resp_{uuid.uuid4().hex[:8]}",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": body.get("model"),
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _chat_reply(body: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    calls = [
        {
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": call["name"], "arguments": _arguments(call)},
        }
        for call in turn.get("calls") or []
    ]
    message: dict[str, Any] = {"role": "assistant", "content": None if calls else turn.get("text", "DONE")}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": f"chat_{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model"),
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls" if calls else "stop",
            "logprobs": None,
            "message": message,
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
