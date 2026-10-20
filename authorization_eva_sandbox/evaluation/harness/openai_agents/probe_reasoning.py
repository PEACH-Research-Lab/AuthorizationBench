"""Live check: does reasoning state survive three sequential tool calls?

Run one setting at a time. Request bodies are written under probe_bodies/
with no headers and no keys. Results go to probe_results.md. Neither file
is committed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import traceback
from pathlib import Path

os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

ROOT = Path(__file__).resolve().parents[4]
BODY_DIR = ROOT / "probe_bodies"
RESULTS = ROOT / "probe_results.md"

OPENROUTER = (
    "sonnet5_low",
    "sonnet5_high",
    "gemini38_flash_low",
    "gemini38_flash_high",
    "glm53_low",
    "glm53_high",
)
REASONING_KEYS = ("reasoning_content", "thinking_blocks", "reasoning_details")
_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]+|Bearer\s+[A-Za-z0-9_\-\.]+)")


def _load_profile(name: str) -> dict:
    """The same read as ``load_llm_profile``.

    ``run_task`` imports the builtin loop, which imports litellm. The native
    probe environment has no litellm, so that import is not the only path.
    """
    try:
        from authorization_eva_sandbox.evaluation.run_task import load_llm_profile
    except ImportError:
        import tomllib

        cfg = tomllib.loads((ROOT / "authorization_eva_sandbox" / "evaluation" / "config.toml").read_text())
        profile = cfg.get("llm", {}).get(name)
        if profile is None:
            raise SystemExit(f"profile [llm.{name}] not found in evaluation/config.toml")
        return profile
    return load_llm_profile(name)


def _redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)[:2000]


def _walk_reasoning(value, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in REASONING_KEYS and item:
                found.add(key)
            if key == "reasoning" and item:
                # The request setting is {"effort": ...}. Anything else is replayed reasoning.
                if not (isinstance(item, dict) and set(item) <= {"effort", "summary", "generate_summary"}):
                    found.add("reasoning")
            if key == "thought_signature" and item:
                found.add("thought_signature")
            if key == "extra_content" and isinstance(item, dict):
                google = item.get("google")
                if isinstance(google, dict) and google.get("thought_signature"):
                    found.add("thought_signature")
            if key == "type" and item == "reasoning":
                found.add("reasoning_item")
            _walk_reasoning(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_reasoning(item, found)


class _BodyLog:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.requests: list[dict] = []
        self._n = 0

    def save(self, request) -> None:
        host = request.url.host
        if host in {"127.0.0.1", "localhost"}:
            return
        content = getattr(request, "content", b"") or b""
        if not content:
            return
        try:
            body = json.loads(content)
        except (json.JSONDecodeError, UnicodeError):
            return
        self._n += 1
        path = self.directory / f"{self._n:02d}.json"
        path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        self.requests.append(body)

    def save_dict(self, body: dict) -> None:
        self._n += 1
        path = self.directory / f"{self._n:02d}.json"
        path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        self.requests.append(body)


def _install_litellm_body_log(log: _BodyLog):
    """Save the LiteLLM call kwargs. The HTTP client does not expose the body on send()."""
    import litellm
    from litellm.integrations.custom_logger import CustomLogger

    class _BodyLogger(CustomLogger):
        def log_pre_api_call(self, model, messages, kwargs):
            body = {
                "model": kwargs.get("model"),
                "messages": kwargs.get("messages"),
                "tools": kwargs.get("tools"),
                "extra_body": kwargs.get("extra_body"),
                "reasoning_effort": kwargs.get("reasoning_effort"),
                "max_tokens": kwargs.get("max_tokens"),
                "parallel_tool_calls": kwargs.get("parallel_tool_calls"),
            }
            log.save_dict(body)

    logger = _BodyLogger()
    litellm.callbacks.append(logger)
    return logger


def _install_body_log(log: _BodyLog) -> list[tuple[object, str, object]]:
    patches = []

    def _wrap(module, names):
        for name in names:
            client = getattr(module, name, None)
            if client is None or not hasattr(client, "send"):
                continue
            original = client.send

            if asyncio.iscoroutinefunction(original):
                async def send(self, request, *args, _original=original, **kwargs):
                    log.save(request)
                    return await _original(self, request, *args, **kwargs)
            else:
                def send(self, request, *args, _original=original, **kwargs):
                    log.save(request)
                    return _original(self, request, *args, **kwargs)

            setattr(client, "send", send)
            patches.append((client, "send", original))

    try:
        import httpx
    except ImportError:
        httpx = None
    if httpx is not None:
        _wrap(httpx, ("Client", "AsyncClient"))
    try:
        import httpx2
    except ImportError:
        httpx2 = None
    if httpx2 is not None:
        _wrap(httpx2, ("Client", "AsyncClient"))
    return patches


def _restore(patches) -> None:
    for obj, name, original in patches:
        setattr(obj, name, original)


def _tokens(usage) -> tuple[object, object, object]:
    if usage is None:
        return None, None, None
    details = getattr(usage, "output_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details else None
    return (
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
        reasoning,
    )


def _replayed(this_request: dict) -> str:
    """Reasoning fields on this request. Call 1 has no prior turn, so later calls are the replay."""
    found: set[str] = set()
    _walk_reasoning(this_request, found)
    if not found:
        return "none"
    return ", ".join(sorted(found))


async def _run_one(profile_name: str, chat_backend: str, log: _BodyLog) -> dict:
    from agents import Agent, Runner, function_tool

    from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
        CostRecorder,
        disable_tracing,
        model_for_profile,
        run_config,
    )

    disable_tracing()

    @function_tool
    def lookup(step: int) -> str:
        """Return the clue for this step. Call step 1, then 2, then 3, one call at a time."""
        return {1: "red", 2: "blue", 3: "green"}.get(int(step), "none")

    profile = _load_profile(profile_name)
    model, settings, info = model_for_profile(profile, chat_backend=chat_backend)
    # parallel_tool_calls is omitted. With the profile's require_parameters
    # lock, OpenRouter rejects the Anthropic endpoint when that field is present.
    agent = Agent(
        name="probe",
        instructions=(
            "Call the lookup tool three times, one call per turn, in this order: "
            "step 1, then step 2, then step 3. Never call it more than once in a turn. "
            "After the third result, answer with the three clue words separated by commas."
        ),
        model=model,
        model_settings=settings,
        tools=[lookup],
    )
    recorder = CostRecorder()
    row: dict = {
        "profile": profile_name,
        "backend": chat_backend if info.api_path != "responses" else "responses",
        "api_path": info.api_path,
        "calls": [],
        "error": None,
    }
    before = len(log.requests)
    try:
        with recorder.start():
            result = await Runner.run(
                agent,
                "Look up the three clues in order and then answer.",
                max_turns=8,
                run_config=run_config(),
            )
        responses = list(getattr(result, "raw_responses", []) or [])
    except Exception as exc:  # noqa: BLE001 — the probe records the failure and continues
        responses = []
        row["error"] = f"{type(exc).__name__}: {_redact(str(exc))}"
        row["trace"] = _redact(traceback.format_exc())
    bodies = log.requests[before:]
    costs = recorder.calls()
    count = max(len(responses), len(bodies), len(costs), 1 if row["error"] else 0)
    for index in range(count):
        usage = responses[index].usage if index < len(responses) else None
        input_tokens, output_tokens, reasoning_tokens = _tokens(usage)
        cost = costs[index].cost_usd if index < len(costs) else None
        if usage is None and index < len(costs):
            recorded = costs[index].usage
            input_tokens = recorded.get("input_tokens")
            output_tokens = recorded.get("output_tokens")
            reasoning_tokens = recorded.get("reasoning_tokens")
            replay = "n/a"
        if index < len(bodies):
            replay = "none" if index == 0 else _replayed(bodies[index])
        status = "ok" if index < len(responses) or (row["error"] is None and index < len(bodies)) else "error"
        if row["error"] and index >= len(responses):
            status = "error"
        row["calls"].append({
            "n": index + 1,
            "status": status,
            "reasoning_tokens": reasoning_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "replayed": replay,
        })
    if row["error"] and not row["calls"]:
        row["calls"].append({
            "n": 1, "status": "error", "reasoning_tokens": None,
            "input_tokens": None, "output_tokens": None, "cost_usd": None, "replayed": "n/a",
        })
    numeric = [call["cost_usd"] for call in row["calls"] if isinstance(call["cost_usd"], (int, float))]
    row["cost_total"] = recorder.total()
    if row["cost_total"] is None and numeric and len(numeric) == len(row["calls"]):
        row["cost_total"] = sum(numeric)
    return row


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _row_markdown(row: dict) -> str:
    calls = {call["n"]: call for call in row["calls"]}
    def cell(n: int, key: str) -> str:
        call = calls.get(n)
        if call is None:
            return "—"
        if key == "status" and row["error"] and call["status"] == "error":
            return "error"
        return _fmt(call.get(key))

    replay = []
    for n in (2, 3):
        call = calls.get(n)
        replay.append(call["replayed"] if call else "—")
    status = " / ".join(cell(n, "status") for n in (1, 2, 3))
    reasoning = " / ".join(cell(n, "reasoning_tokens") for n in (1, 2, 3))
    return (
        f"| {row['backend']} | {row['profile']} | {status} | {reasoning} | "
        f"{replay[0]}; {replay[1]} | {_fmt(row['cost_total'])} |"
    )


def _merge_rows(new_rows: list[dict]) -> list[dict]:
    path = ROOT / "probe_rows.json"
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    by_key = {(row["backend"], row["profile"]): row for row in stored}
    for row in new_rows:
        by_key[(row["backend"], row["profile"])] = row
    order = [("litellm", name) for name in OPENROUTER]
    order += [("native", name) for name in OPENROUTER]
    order.append(("responses", "gpt5_6_sol_high"))
    merged = [by_key[key] for key in order if key in by_key]
    extras = [row for key, row in by_key.items() if key not in set(order)]
    path.write_text(json.dumps(merged + extras, indent=2), encoding="utf-8")
    return merged + extras


def _write_results(rows: list[dict]) -> None:
    lines = [
        "# Reasoning-state probe",
        "",
        "| backend | setting | calls 1-3 | reasoning tokens 1-3 | state sent back (call 2; call 3) | cost total |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(_row_markdown(row).rstrip() for row in rows)
    lines.append("")
    for row in rows:
        if not row.get("error"):
            continue
        lines.append(f"## {row['backend']} {row['profile']}")
        lines.append("")
        lines.append(_redact(row["error"]))
        lines.append("")
    RESULTS.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("litellm", "native"), required=True)
    parser.add_argument("--gpt", action="store_true", help="run gpt5_6_sol_high only")
    parser.add_argument("--only", action="append", default=[], help="profile name, repeatable")
    args = parser.parse_args()
    names = ["gpt5_6_sol_high"] if args.gpt else (args.only or list(OPENROUTER))
    rows = []
    for name in names:
        directory = BODY_DIR / args.backend / name
        log = _BodyLog(directory)
        litellm_logger = _install_litellm_body_log(log) if args.backend == "litellm" else None
        patches = [] if litellm_logger is not None else _install_body_log(log)
        print(f"probe {args.backend} {name}", flush=True)
        try:
            row = asyncio.run(_run_one(name, args.backend, log))
        finally:
            _restore(patches)
            if litellm_logger is not None:
                import litellm

                if litellm_logger in litellm.callbacks:
                    litellm.callbacks.remove(litellm_logger)
        rows.append(row)
        _write_results(_merge_rows(rows))
        print(
            f"  calls={len(row['calls'])} error={row['error'] is not None} "
            f"cost={row['cost_total']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
