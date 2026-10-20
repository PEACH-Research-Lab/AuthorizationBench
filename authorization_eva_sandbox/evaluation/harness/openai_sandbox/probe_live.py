"""Live 3-tool-call SandboxAgent smoke on the 8 benchmark profiles (spike question 4).

Reads ``evaluation/config.toml``; never prints or stores a key. One empty
agent container, one attached sandbox session, profiles run one at a time,
cheapest first. Each run: ``exec_command``, ``apply_patch``, ``finish``
(``finish`` stops the segment, so three model calls). Stops before the next
profile once recorded spend reaches ``SPEND_STOP_USD``.

Per call it records the outgoing effort, provider lock and output cap (from
the ``litellm.acompletion`` / ``responses.create`` arguments, keys dropped),
usage including reasoning tokens, and LiteLLM ``response_cost`` or the
Responses price. Writes ``spike_captures/live/summary.json``.

    .venv-openai-agents/bin/python -m \
        authorization_eva_sandbox.evaluation.harness.openai_sandbox.probe_live
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from typing import Any

from authorization_eva_sandbox.evaluation.harness.openai_sandbox import spike_common as sc

OUT = sc.CAPTURES / "live"
ORDER = (
    "gemini38_flash_low", "gemini38_flash_high",
    "glm53_low", "glm53_high",
    "sonnet5_low", "sonnet5_high",
    "gpt5_6_sol_low", "gpt5_6_sol_high",
)
SPEND_STOP_USD = 0.17
INSTRUCTIONS = (
    "This run is a harness smoke test. Follow the user's steps exactly, one tool call per turn.\n\n"
    + sc.ENVIRONMENT_NOTES
)


def _task(label: str) -> str:
    return (
        "Step 1: call exec_command with cmd `echo step-one`. "
        f"Step 2: after its result, call apply_patch to create the file smoke_{label}.txt "
        f"whose only line is `smoke-{label}`. "
        "Step 3: after that result, call finish with message DONE. "
        "Make exactly these three tool calls and no others."
    )


_KEEP = ("model", "reasoning", "reasoning_effort", "max_output_tokens", "max_tokens",
         "max_completion_tokens", "extra_body", "provider", "include", "store", "stream")


def _install_wire_capture(sink: list[dict[str, Any]]) -> list[tuple[str, Any, Any]]:
    import litellm
    from openai.resources.responses.responses import AsyncResponses

    patches: list[tuple[str, Any, Any]] = []
    original_acompletion = litellm.acompletion

    async def acompletion(*args: Any, **kwargs: Any) -> Any:
        record = {"api": "litellm", **{k: kwargs.get(k) for k in _KEEP if k in kwargs}}
        response = await original_acompletion(*args, **kwargs)
        usage = getattr(response, "usage", None)
        record["openrouter_cost"] = getattr(usage, "cost", None) if usage is not None else None
        record["served_by"] = getattr(response, "provider", None)
        hidden = getattr(response, "_hidden_params", None) or {}
        record["litellm_response_cost"] = hidden.get("response_cost")
        sink.append(record)
        return response

    litellm.acompletion = acompletion
    patches.append(("litellm", litellm, original_acompletion))
    original_create = AsyncResponses.create

    async def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        record = {"api": "responses", **{k: kwargs.get(k) for k in _KEEP if k in kwargs}}
        response = await original_create(self, *args, **kwargs)
        record["status"] = getattr(response, "status", None)
        sink.append(record)
        return response

    AsyncResponses.create = create
    patches.append(("responses", AsyncResponses, original_create))
    return patches


def _remove_wire_capture(patches: list[tuple[str, Any, Any]]) -> None:
    for kind, owner, original in patches:
        if kind == "litellm":
            owner.acompletion = original
        else:
            owner.create = original


async def _one(profile_name: str, profile: dict[str, Any], tc: sc.TaskContainer,
               session: Any, wire: list[dict[str, Any]]) -> dict[str, Any]:
    from agents import RunHooks, Runner

    from authorization_eva_sandbox.evaluation.harness.agent_container import IMAGE_USER
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
        CostRecorder,
        model_for_profile,
    )
    from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
        WorkspaceTools,
        function_tools,
    )

    chat = sc.is_chat_profile(profile)
    model, settings, info = model_for_profile(profile, chat_backend="litellm")
    workspace = WorkspaceTools(tc.container.name, user=IMAGE_USER, cwd="/workspace")
    agent = sc.build_agent(
        instructions=INSTRUCTIONS, model=model, settings=settings,
        tools=function_tools(["finish"], workspace), mcp_server=None, chat_path=chat,
    )
    calls: list[dict[str, Any]] = []

    class _Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):  # noqa: ANN001
            calls.append({"tool": getattr(tool, "name", None), "result": str(result)[:200]})

    start_wire = len(wire)
    recorder = CostRecorder()
    error = None
    result = None
    started = time.time()
    with recorder.start():
        try:
            result = await Runner.run(
                agent, _task(profile_name), max_turns=5, hooks=_Hooks(),
                run_config=sc.sandbox_run_config(session),
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
    workspace.close()
    usage = [call.usage for call in recorder.calls()]
    costs = [call.cost_usd for call in recorder.calls()]
    file_text = tc.exec(f"cat /workspace/smoke_{profile_name}.txt 2>&1")
    items = []
    if result is not None:
        (OUT / f"{profile_name}_items.json").write_text(
            json.dumps(result.to_input_list(), default=str, indent=1)
        )
        for item in result.new_items:
            raw = getattr(item, "raw_item", None)
            raw_type = raw.get("type") if isinstance(raw, dict) else getattr(raw, "type", None)
            items.append(f"{type(item).__name__}:{raw_type}")
    return {
        "api_path": info.api_path,
        "model_id": info.model_id,
        "effort": info.effort,
        "cap": info.max_output_tokens,
        "provider_lock": info.provider_lock,
        "error": error,
        "seconds": round(time.time() - started, 1),
        "tool_calls": calls,
        "file": file_text,
        "final_output": str(getattr(result, "final_output", ""))[:200] if result else None,
        "items": items,
        "usage": usage,
        "cost_usd": costs,
        "cost_total": sum(c or 0.0 for c in costs),
        "wire": wire[start_wire:],
    }


def _scan_for_secrets(tc: sc.TaskContainer, secrets: list[str]) -> dict[str, Any]:
    hits: dict[str, Any] = {"workspace": [], "container_new_files": [], "captures": []}
    for path in (tc.host_copy or sc.CAPTURES).rglob("*"):
        if path.is_file():
            text = path.read_bytes().decode("utf-8", "replace")
            if any(s and s in text for s in secrets):
                hits["workspace"].append(str(path))
    listing = tc.exec(
        "find / -xdev -newer /tmp/osb_marker -type f -not -path '/proc/*' 2>/dev/null"
    ).splitlines()
    for path in listing:
        proc = subprocess.run(
            ["docker", "exec", tc.container_id, "cat", path], capture_output=True, check=False,
        )
        text = proc.stdout.decode("utf-8", "replace")
        if any(s and s in text for s in secrets):
            hits["container_new_files"].append(path)
    env = tc.exec("env")
    hits["container_env"] = any(s and s in env for s in secrets)
    for path in OUT.rglob("*"):
        if path.is_file() and any(s and s in path.read_text() for s in secrets):
            hits["captures"].append(str(path))
    hits["container_files_checked"] = listing
    return hits


async def main() -> None:
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import disable_tracing

    disable_tracing()
    OUT.mkdir(parents=True, exist_ok=True)
    profiles = sc.live_profiles()
    secrets = sorted({str(profiles[n].get("api_key") or "") for n in ORDER} - {""})
    wire: list[dict[str, Any]] = []
    patches = _install_wire_capture(wire)
    summary: dict[str, Any] = {"runs": {}, "stopped_before": None}
    tc = sc.TaskContainer(None).start()
    spent = 0.0
    try:
        tc.exec("touch /tmp/osb_marker && sleep 1")
        session = await sc.attach_session(tc)
        for name in ORDER:
            if spent >= SPEND_STOP_USD:
                summary["stopped_before"] = name
                break
            run = await _one(name, profiles[name], tc, session, wire)
            summary["runs"][name] = run
            spent += run["cost_total"]
            summary["spent_usd"] = spent
            _save(summary, secrets)
            print(f"{name}: error={run['error']} calls={[c['tool'] for c in run['tool_calls']]} "
                  f"cost={run['cost_total']:.5f} spent={spent:.5f}", flush=True)
        summary["secrets_found"] = _scan_for_secrets(tc, secrets)
    finally:
        _remove_wire_capture(patches)
        tc.close()
    _save(summary, secrets)
    print(f"spent_usd={spent:.5f}")


def _save(summary: dict[str, Any], secrets: list[str]) -> None:
    text = json.dumps(summary, indent=1, default=str)
    if any(s in text for s in secrets):
        raise SystemExit("a credential reached the summary; not writing it")
    (OUT / "summary.json").write_text(text + "\n")


if __name__ == "__main__":
    asyncio.run(main())
