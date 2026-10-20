"""Step 1: does native Compaction survive the chat path?

Function-tool ``apply_patch``, ``StaticCompactionPolicy(threshold=2000)``,
one live call each for Sonnet, Gemini and GLM (low). The sandbox base prompt
is already past 2000 tokens, so a provider that honors the field should
compact on the first request.

Writes ``spike_captures/compaction/summary.json``. Keys are never stored.

    .venv-openai-agents/bin/python -m \
        authorization_eva_sandbox.evaluation.harness.openai_sandbox.probe_compaction
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from authorization_eva_sandbox.evaluation.harness.openai_sandbox import spike_common as sc

OUT = sc.CAPTURES / "compaction"
THRESHOLD = 2000
ORDER = ("sonnet5_low", "gemini38_flash_low", "glm53_low")
CLUE_KEYS = {"compaction", "context_management", "applied_edits", "compact_threshold"}
TASK = (
    "Call finish now with message DONE. Do not call exec_command or apply_patch."
)


def _short(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value[:240]
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): _short(v, depth + 1) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        return [_short(v, depth + 1) for v in value[:8]]
    return value


def _clues(obj: Any, found: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in CLUE_KEYS:
                found.append({str(key): _short(value)})
            else:
                _clues(value, found, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:40]:
            _clues(item, found, depth + 1)


def _install_wire(sink: list[dict[str, Any]]) -> Any:
    import litellm

    original = litellm.acompletion

    async def acompletion(*args: Any, **kwargs: Any) -> Any:
        extra = kwargs.get("extra_body") if isinstance(kwargs.get("extra_body"), dict) else {}
        record: dict[str, Any] = {
            "model": kwargs.get("model"),
            "context_management": kwargs.get("context_management") or extra.get("context_management"),
            "apply_patch_type": None,
        }
        for tool in kwargs.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            function_name = (tool.get("function") or {}).get("name")
            if function_name == "apply_patch" or tool.get("name") == "apply_patch":
                record["apply_patch_type"] = tool.get("type")
        try:
            response = await original(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            sink.append(record)
            raise
        usage = getattr(response, "usage", None)
        record["prompt_tokens"] = getattr(usage, "prompt_tokens", None) if usage else None
        record["served_by"] = getattr(response, "provider", None)
        hidden = getattr(response, "_hidden_params", None) or {}
        record["litellm_response_cost"] = hidden.get("response_cost")
        dumped: Any = None
        if hasattr(response, "model_dump"):
            dumped = response.model_dump()
        clues: list[dict[str, Any]] = []
        _clues(dumped, clues)
        record["response_clues"] = clues
        sink.append(record)
        return response

    litellm.acompletion = acompletion
    return original


async def _one(name: str, profile: dict[str, Any], tc: sc.TaskContainer, session: Any,
              wire: list[dict[str, Any]]) -> dict[str, Any]:
    from agents import Runner
    from agents.sandbox.capabilities import Compaction, Filesystem, Shell, StaticCompactionPolicy

    from authorization_eva_sandbox.evaluation.harness.agent_container import IMAGE_USER
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
        CostRecorder,
        model_for_profile,
    )
    from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
        WorkspaceTools,
        function_tools,
    )

    model, settings, info = model_for_profile(profile, chat_backend="litellm")
    workspace = WorkspaceTools(tc.container.name, user=IMAGE_USER, cwd="/workspace")
    agent = sc.build_agent(
        instructions="Harness compaction probe. Call finish when asked.",
        model=model,
        settings=settings,
        tools=function_tools(["finish"], workspace),
        mcp_server=None,
        chat_path=True,
        caps=[
            Shell(),
            Filesystem(configure_tools=sc.chat_apply_patch),
            Compaction(policy=StaticCompactionPolicy(threshold=THRESHOLD)),
        ],
    )
    start = len(wire)
    recorder = CostRecorder()
    error = None
    result = None
    item_clues: list[dict[str, Any]] = []
    started = time.time()
    with recorder.start():
        try:
            result = await Runner.run(
                agent, TASK, max_turns=2, run_config=sc.sandbox_run_config(session),
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
    workspace.close()
    if result is not None:
        _clues(result.to_input_list(), item_clues)
    costs = [call.cost_usd for call in recorder.calls()]
    return {
        "model_id": info.model_id,
        "provider_lock": info.provider_lock,
        "threshold": THRESHOLD,
        "error": error,
        "seconds": round(time.time() - started, 1),
        "final_output": str(getattr(result, "final_output", ""))[:200] if result else None,
        "item_clues": item_clues,
        "cost_total": sum(c or 0.0 for c in costs),
        "wire": wire[start:],
    }


async def main() -> None:
    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import disable_tracing

    disable_tracing()
    OUT.mkdir(parents=True, exist_ok=True)
    profiles = sc.live_profiles()
    secrets = sorted({str(profiles[n].get("api_key") or "") for n in ORDER} - {""})
    wire: list[dict[str, Any]] = []
    original = _install_wire(wire)
    summary: dict[str, Any] = {"threshold": THRESHOLD, "runs": {}}
    tc = sc.TaskContainer(None).start()
    spent = 0.0
    try:
        session = await sc.attach_session(tc)
        for name in ORDER:
            run = await _one(name, profiles[name], tc, session, wire)
            summary["runs"][name] = run
            spent += run["cost_total"]
            print(
                f"{name}: error={run['error']} prompt_tokens="
                f"{[w.get('prompt_tokens') for w in run['wire']]} "
                f"clues={bool(run['item_clues'] or any(w.get('response_clues') for w in run['wire']))} "
                f"cost={run['cost_total']:.5f}",
                flush=True,
            )
    finally:
        import litellm

        litellm.acompletion = original
        tc.close()
    summary["spent_usd"] = spent
    text = json.dumps(summary, indent=1, default=str)
    if any(secret and secret in text for secret in secrets):
        raise SystemExit("a credential reached the summary; not writing it")
    (OUT / "summary.json").write_text(text + "\n")
    print(f"spent_usd={spent:.5f}")


if __name__ == "__main__":
    asyncio.run(main())
