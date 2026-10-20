"""Container, tools and isolation probe (spike questions 1, 3 and 6). Offline.

Starts T0015-j-c-h's container the way the plain adapter does, tries the
public ``DockerSandboxClient`` routes, attaches a ``DockerSandboxSession`` to
the running container, and drives one scripted SandboxAgent run per API path
(Responses and LiteLLM chat) through a local recorder. Every SDK tool and our
own tools are called. Writes ``spike_captures/container/summary.json``.

Run from the worktree root, outside the Cursor sandbox (Docker and a pty):

    .venv-openai-agents/bin/python -m \
        authorization_eva_sandbox.evaluation.harness.openai_sandbox.probe_container
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from authorization_eva_sandbox.evaluation.harness.openai_sandbox import spike_common as sc
from authorization_eva_sandbox.evaluation.harness.openai_sandbox.recorder import Recorder

TASK = "T0015-j-c-h"
OUT = sc.CAPTURES / "container"
PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


def _osb_containers() -> list[str]:
    proc = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}} {{.Names}}"],
        capture_output=True, text=True, check=False,
    )
    return sorted(line for line in proc.stdout.splitlines() if line.strip())


def _inspect(container_id: str) -> dict[str, Any]:
    proc = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=True)
    data = json.loads(proc.stdout)[0]
    config = data.get("Config") or {}
    host = data.get("HostConfig") or {}
    return {
        "image": config.get("Image"),
        "entrypoint": config.get("Entrypoint"),
        "cmd": config.get("Cmd"),
        "user": config.get("User"),
        "working_dir": config.get("WorkingDir"),
        "env_keys": sorted(item.split("=", 1)[0] for item in config.get("Env") or []),
        "home": next((item for item in config.get("Env") or [] if item.startswith("HOME=")), None),
        "mounts": [
            {"type": m.get("Type"), "destination": m.get("Destination"), "rw": m.get("RW")}
            for m in data.get("Mounts") or []
        ],
        "network_mode": host.get("NetworkMode"),
        "extra_hosts": host.get("ExtraHosts"),
        "platform": data.get("Platform"),
    }


class _HostPing(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _HostPing.hits.append(self.path)
        data = b"HOST_REACHED"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


async def _client_create_evidence(image: str) -> dict[str, Any]:
    """What ``DockerSandboxClient.create`` starts when asked for our image."""
    from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
    from agents.sandbox.snapshot import NoopSnapshotSpec

    client = DockerSandboxClient(sc.docker_client())
    session = await client.create(
        manifest=sc.session_manifest(),
        options=DockerSandboxClientOptions(image=image),
        snapshot=NoopSnapshotSpec(),
    )
    container_id = session.state.container_id
    try:
        return {"container": _inspect(container_id)}
    finally:
        await client.delete(session)


def _last_tool_output(body: dict[str, Any]) -> str:
    for item in reversed(body.get("input") or []):
        if isinstance(item, dict) and item.get("type") in ("function_call_output", "custom_tool_call_output"):
            out = item.get("output")
            return out if isinstance(out, str) else json.dumps(out)
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "tool":
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def _pty_id(body: dict[str, Any]) -> int:
    match = re.search(r"session ID (\d+)", _last_tool_output(body))
    return int(match.group(1)) if match else -1


def _script(label: str, *, chat: bool, port: int) -> list[dict[str, Any]]:
    patch = f"*** Begin Patch\n*** Add File: spike_patch_{label}.txt\n+PATCH_MARKER_{label}\n*** End Patch\n"
    patch_call = (
        {"name": "apply_patch", "arguments": {"input": patch}}
        if chat else {"name": "apply_patch", "arguments": patch, "custom": True}
    )
    return [
        {"calls": [{"name": "exec_command", "arguments": {
            "cmd": "id -un; pwd; echo HOME=$HOME; readlink /proc/$$/exe; command -v npm; ls -A /workspace",
        }}]},
        {"calls": [{"name": "exec_command", "arguments": {
            "cmd": "npm list; tail -n 1 /tmp/tool_invocations.jsonl",
        }}]},
        {"calls": [{"name": "exec_command", "arguments": {"cmd": "cat", "tty": True, "yield_time_ms": 500}}]},
        {"dynamic": lambda body: {"calls": [{"name": "write_stdin", "arguments": {
            "session_id": _pty_id(body), "chars": "hello-pty\n", "yield_time_ms": 500,
        }}]}},
        {"dynamic": lambda body: {"calls": [{"name": "write_stdin", "arguments": {
            "session_id": _pty_id(body), "chars": "\u0003", "yield_time_ms": 300,
        }}]}},
        {"calls": [patch_call]},
        {"calls": [{"name": "view_image", "arguments": {"path": "spike.png"}}]},
        {"calls": [{"name": "terminal", "arguments": {"command": "echo OURS_TERMINAL; whoami; pwd"}}]},
        {"calls": [{"name": "read_file", "arguments": {"file_path": f"/workspace/spike_patch_{label}.txt"}}]},
        {"calls": [{"name": "write_file", "arguments": {
            "file_path": f"/workspace/spike_write_{label}.txt", "content": f"WRITE_MARKER_{label}\n",
        }}]},
        {"calls": [{"name": "exec_command", "arguments": {
            "cmd": f"curl -s -m 5 http://host.docker.internal:{port}/ping-{label}",
        }}]},
        {"calls": [{"name": "finish", "arguments": {"message": f"done {label}"}}]},
    ]


def _request_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for tool in body.get("tools") or []:
        if "function" in tool:
            fn = tool["function"]
            out.append({"type": tool.get("type"), "name": fn.get("name"), "parameters": fn.get("parameters")})
        else:
            out.append({
                "type": tool.get("type"), "name": tool.get("name"),
                "parameters": tool.get("parameters"), "format": tool.get("format"),
            })
    return out


async def _scripted_run(tc: sc.TaskContainer, session: Any, recorder: Recorder,
                        profile_name: str, label: str, port: int) -> dict[str, Any]:
    from agents import RunHooks, Runner

    from authorization_eva_sandbox.evaluation.harness.agent_container import IMAGE_USER
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import model_for_profile
    from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
        WorkspaceTools,
        function_tools,
    )

    profile = {**sc.example_profiles()[profile_name], "api_key": "x", "base_url": recorder.base_url}
    chat = sc.is_chat_profile(profile)
    model, settings, info = model_for_profile(profile, chat_backend="litellm")
    workspace = WorkspaceTools(tc.container.name, user=IMAGE_USER, cwd="/workspace")
    ours = function_tools(["read_file", "write_file", "terminal", "finish"], workspace)
    agent = sc.build_agent(
        instructions="SPIKE_OUR_INSTRUCTIONS", model=model, settings=settings,
        tools=ours, mcp_server=None, chat_path=chat,
    )
    events: list[dict[str, Any]] = []

    class _Hooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):  # noqa: ANN001
            events.append({"hook": "tool_start", "tool": getattr(tool, "name", None),
                           "class": type(tool).__name__})

        async def on_tool_end(self, context, agent, tool, result):  # noqa: ANN001
            text = result if isinstance(result, str) else repr(result)
            events.append({"hook": "tool_end", "tool": getattr(tool, "name", None),
                           "result": text[:600]})

    recorder.load(_script(label, chat=chat, port=port))
    result = await Runner.run(
        agent, "SPIKE_USER_TASK", max_turns=20, hooks=_Hooks(),
        run_config=sc.sandbox_run_config(session),
    )
    workspace.close()
    items = []
    for item in result.new_items:
        raw = getattr(item, "raw_item", None)
        raw_type = raw.get("type") if isinstance(raw, dict) else getattr(raw, "type", None)
        name = raw.get("name") if isinstance(raw, dict) else getattr(raw, "name", None)
        output = getattr(item, "output", None)
        items.append({
            "item": type(item).__name__, "raw_type": raw_type, "name": name,
            "output": (output if isinstance(output, str) else repr(output))[:300] if output is not None else None,
        })
    first = recorder.requests[0]["body"]
    instructions = first.get("instructions")
    if instructions is None:
        messages = first.get("messages") or []
        instructions = messages[0].get("content") if messages and messages[0].get("role") == "system" else None
    if isinstance(instructions, list):
        instructions = "\n".join(block.get("text", "") for block in instructions if isinstance(block, dict))
    view_image_followup = None
    if len(recorder.requests) > 7:
        body = recorder.requests[7]["body"]
        view_image_followup = (body.get("input") or body.get("messages") or [])[-1]
    return {
        "profile": profile_name,
        "api_path": info.api_path,
        "requests": len(recorder.requests),
        "paths": sorted({r["path"] for r in recorder.requests}),
        "tools_sent": _request_tools(first),
        "instructions_chars": len(instructions or ""),
        "instructions_head": (instructions or "")[:120],
        "instructions_tail": (instructions or "")[-400:],
        "context_management_in_body": "context_management" in first,
        "hooks": events,
        "new_items": items,
        "input_list_types": [
            (i.get("type") or i.get("role")) if isinstance(i, dict) else type(i).__name__
            for i in result.to_input_list()
        ],
        "view_image_followup_item": json.dumps(view_image_followup, default=str)[:600],
        "final_output": str(result.final_output)[:200],
    }


async def main() -> None:
    import agents.tracing.processors as processors

    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import disable_tracing

    disable_tracing()
    exported: list[int] = []
    original_export = processors.BackendSpanExporter.export

    def _export(self, items):  # noqa: ANN001
        exported.append(len(items))
        return original_export(self, items)

    processors.BackendSpanExporter.export = _export

    OUT.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"snapshot_dir_exists_before": sc.SNAPSHOT_DEFAULT_DIR.exists()}
    before = _osb_containers()
    host = ThreadingHTTPServer(("127.0.0.1", 0), _HostPing)
    threading.Thread(target=host.serve_forever, daemon=True).start()
    port = host.server_port
    tc = sc.TaskContainer(TASK).start()
    try:
        summary["our_container"] = _inspect(tc.container_id)
        summary["wrapper"] = tc.exec("ls -l /usr/local/bin/npm /workspace/bin/npm; rm -f /tmp/tool_invocations.jsonl; echo ok")
        tc.exec(f"echo {PNG_1PX} | base64 -d > /workspace/spike.png")
        host_before = sc.tree_listing(tc.host_copy)
        summary["client_resume_no_grant"] = await sc.try_client_resume(tc, grant_workspace=False)
        summary["client_resume_workspace_grant"] = await sc.try_client_resume(tc, grant_workspace=True)
        summary["client_create"] = await _client_create_evidence(tc.image)
        summary["containers_after_client_probes"] = sorted(set(_osb_containers()) - set(before))
        tc.exec("touch /tmp/osb_marker && sleep 1")
        session = await sc.attach_session(tc)
        summary["attach"] = {
            "type": type(session).__name__,
            "supports_pty": session.supports_pty(),
            "workspace_root_ready": session.state.workspace_root_ready,
            "new_containers": sorted(set(_osb_containers()) - set(before)),
            "files_written_by_start": tc.exec(
                "find / -xdev -newer /tmp/osb_marker -type f -not -path '/proc/*' 2>/dev/null | sort"
            ),
            "workspace_changed_by_start": sc.tree_listing(tc.host_copy) != host_before,
        }
        with Recorder(OUT / "requests.jsonl") as recorder:
            summary["runs"] = {
                "responses": await _scripted_run(tc, session, recorder, "gpt5_6_sol_high", "resp", port),
                "litellm_chat": await _scripted_run(tc, session, recorder, "sonnet5_high", "chat", port),
            }
        host_after = sc.tree_listing(tc.host_copy)
        summary["workspace_diff"] = {
            "added": sorted(set(host_after) - set(host_before)),
            "removed": sorted(set(host_before) - set(host_after)),
        }
        summary["file_owners"] = tc.exec("stat -c '%U %n' /workspace/spike_patch_* /workspace/spike_write_*")
        summary["tool_invocations"] = tc.exec("cat /tmp/tool_invocations.jsonl")
        summary["files_written_during_runs"] = tc.exec(
            "find / -xdev -newer /tmp/osb_marker -type f -not -path '/proc/*' "
            "-not -path '/workspace/node_modules/*' 2>/dev/null | sort"
        )
        summary["host_pings"] = list(_HostPing.hits)
        summary["container_running_after_runs"] = await session.running()
        summary["containers_after_runs"] = sorted(set(_osb_containers()) - set(before))
        summary["container_env_has_key_like_values"] = bool(re.search(
            r"(sk-|OPENAI|OPENROUTER|ANTHROPIC)", tc.exec("env"),
        ))
    finally:
        tc.close()
        host.shutdown()
        processors.BackendSpanExporter.export = original_export
    summary["snapshot_dir_exists_after"] = sc.SNAPSHOT_DEFAULT_DIR.exists()
    summary["trace_exports"] = exported
    summary["env_OPENAI_AGENTS_DISABLE_TRACING"] = os.environ.get("OPENAI_AGENTS_DISABLE_TRACING")
    summary["containers_left"] = sorted(set(_osb_containers()) - set(before))
    sc.write_json(OUT / "summary.json", summary)
    print(json.dumps(summary, indent=2, default=str)[:20000])


if __name__ == "__main__":
    start = time.time()
    asyncio.run(main())
    print(f"elapsed {time.time() - start:.1f}s")
