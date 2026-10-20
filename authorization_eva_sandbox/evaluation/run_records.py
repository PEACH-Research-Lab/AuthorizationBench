"""Benchmark run records: one row per run of a benchmark model setting.

    python -m authorization_eva_sandbox.evaluation.run_records          # writes outputs/benchmark_runs.html
    python -m authorization_eva_sandbox.evaluation.run_records pending  # runs with no summary yet
    python -m authorization_eva_sandbox.evaluation.run_records digest <run id>   # one run, step by step
    (live) the dashboard serves the same table at http://localhost:8765/runs

Clicking a run on the page opens its summary: <run folder>/run_summary.md, written after
the run by a reviewer who read the digest.

Rows come from the post-hoc reports. OpenHands reports live under
detectors/post_hoc_eval_report/openhands-native/<setting>/ or
openhands-sealed/<setting>/. Another harness is one directory up from the
setting: detectors/post_hoc_eval_report/<harness>/<setting>/. A leftover
report directly under <setting>/ is still read as OpenHands. Each row has a
harness field; OpenHands runs are "openhands". Each row also has the prompt
condition from metadata.prompt ("sealed" or "native"); a run that predates the
field is "sealed". A run with no report yet
(still running, or crashed before post-hoc) is not listed. Notes for single
runs live in outputs/benchmark_run_notes.json as {"<run folder name>": "note"}.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
import statistics
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.evaluation.detectors.traj_renderer import (
    attachment_evidence_text,
)
from authorization_eva_sandbox.evaluation.harness.native_side_effects import summary_line
from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    NATIVE,
    PROMPT_CONDITIONS,
    SEALED,
    recorded_condition,
    recorded_fingerprint,
)

SANDBOX_ROOT = Path(__file__).resolve().parents[1]
REPORTS = SANDBOX_ROOT / "evaluation" / "detectors" / "post_hoc_eval_report"
OUTPUTS = SANDBOX_ROOT / "outputs"
NOTES = OUTPUTS / "benchmark_run_notes.json"
SUPERSEDED = OUTPUTS / "benchmark_run_superseded.json"
SETTINGS = ("gpt-5.6-sol-low", "gpt-5.6-sol-high", "claude-sonnet-5-low", "claude-sonnet-5-high",
            "gemini-3.8-flash-low", "gemini-3.8-flash-high", "glm-5.3-low", "glm-5.3-high")
# OpenHands reports are split by prompt. Both folders still record harness
# "openhands"; the prompt comes from the trajectory.
_OPENHANDS_REPORT_DIRS = ("openhands-native", "openhands-sealed")
# Prompt condition when the trajectory is not on this machine (a report
# copied back from another machine). These harnesses have only native runs.
_NATIVE_ONLY_HARNESSES = ("openai_sandbox", "claude_agent", "adk")
SUMMARY_FILE = "run_summary.md"
_NAME = re.compile(r"^output_full_(T\d{4}-[a-z]-[a-z]-[hn])_(.+)_(\d+)\.md$")


def _field(text: str, label: str) -> str:
    m = re.search(rf"^- \*\*{re.escape(label)}\*\*: (.+)$", text, re.M)
    return m.group(1).strip().strip("`") if m else ""


def _answer(text: str, section: str) -> str:
    m = re.search(rf"^## {section}\s*$.*?\*\*answer\*\*: `([^`]+)`", text, re.S | re.M)
    return m.group(1) if m else ""


def _row(path: Path, notes: dict[str, str], harness: str = "openhands",
         fallback_prompt: str = SEALED, expected_prompt: str | None = None) -> dict[str, Any] | None:
    """``expected_prompt`` is the condition the report folder guarantees.

    A trajectory recording another condition is a different run that reuses
    the folder name (a report copied back from another machine), so it is
    not read.
    """
    m = _NAME.match(path.name)
    if not m:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    task, setting, run = m.group(1), m.group(2), int(m.group(3))
    if harness != "openhands" and setting.endswith("_" + harness):
        setting = setting[: -(len(harness) + 1)]
    traj_rel = _field(text, "trajectory")
    traj = (SANDBOX_ROOT.parent / traj_rel) if traj_rel else None
    started, version, outside, ts = "", "", None, 0.0
    prompt, prompt_version = fallback_prompt, fallback_prompt
    data = None
    if traj and traj.is_file():
        data = json.loads(traj.read_text(encoding="utf-8"))
        if expected_prompt and recorded_condition(data.get("metadata") or {}) != expected_prompt:
            data, traj = None, None
    if data is not None:
        from authorization_eva_sandbox.evaluation.detectors.workspace_scope import (
            outside_workspace_accesses,
        )
        if isinstance(data.get("timestamp_start"), (int, float)):
            ts = float(data["timestamp_start"])
            started = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        meta = data.get("metadata") or {}
        version = str((meta.get("bundle") or {}).get("sha256") or "")[:8]
        prompt, prompt_version = recorded_condition(meta), recorded_fingerprint(meta)
        outside = len(outside_workspace_accesses(data))
    cost = _field(text, "cost (provider-reported)").lstrip("$")
    iters = _field(text, "agent iterations")
    folder = Path(traj_rel).parent.name if traj_rel else path.stem
    summary = traj.parent / SUMMARY_FILE if traj else None
    return {
        "id": folder, "traj": str(traj) if traj else "",
        "summary": summary.read_text(encoding="utf-8").strip() if summary and summary.is_file() else "",
        "harness": harness, "prompt": prompt, "prompt_version": prompt_version,
        "setting": setting, "task": task, "run": run,
        "completion": _answer(text, "Completion"), "violation": _answer(text, "Violation"),
        "iterations": iters.split(" (")[0], "termination": _field(text, "termination"),
        "seconds": round(float(_field(text, "duration (s)") or 0)),
        "cost": float(cost) if re.fullmatch(r"[0-9.]+", cost or "") else None,
        "outside": outside,
        "started": started, "ts": ts, "version": version,
        "report": str(path.relative_to(SANDBOX_ROOT)), "note": notes.get(folder, ""),
    }


def collect() -> list[dict[str, Any]]:
    notes = json.loads(NOTES.read_text(encoding="utf-8")) if NOTES.is_file() else {}
    superseded = json.loads(SUPERSEDED.read_text(encoding="utf-8")) if SUPERSEDED.is_file() else {}
    rows: list[dict[str, Any]] = []
    for setting in SETTINGS:
        for path in sorted((REPORTS / setting).glob("output_full_*.md")):
            if (row := _row(path, notes)):
                rows.append(row)
        for bucket in _OPENHANDS_REPORT_DIRS:
            fallback = NATIVE if bucket == "openhands-native" else SEALED
            for path in sorted((REPORTS / bucket / setting).glob("output_full_*.md")):
                if (row := _row(path, notes, fallback_prompt=fallback, expected_prompt=fallback)):
                    rows.append(row)
        if not REPORTS.is_dir():
            continue
        for harness_dir in sorted(REPORTS.iterdir()):
            if not harness_dir.is_dir():
                continue
            name = harness_dir.name
            if (name in SETTINGS or name in _OPENHANDS_REPORT_DIRS
                    or name.startswith("scenario-probe") or name.startswith(".")):
                continue
            fallback = NATIVE if name in _NATIVE_ONLY_HARNESSES else SEALED
            for path in sorted((harness_dir / setting).glob("output_full_*.md")):
                if (row := _row(path, notes, harness=name, fallback_prompt=fallback)):
                    rows.append(row)
    for r in rows:
        r["superseded"] = superseded.get(r["id"], "")
    rows.sort(key=lambda r: (r["task"], SETTINGS.index(r["setting"]), r["harness"],
                             r["prompt"], r["run"]))
    return rows


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f" …[{len(text) - limit} more chars]"


_SUMMARY_CLIP = 1800
_FILE_TOOLS = {"read_file", "file_read", "write_file", "file_edit", "file_editor", "edit",
               "planning_file_editor"}
_STEP_TEXT_KEYS = ("content", "thought", "final_thought", "agent_message", "oracle_response", "message")


def _json_string(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return None


def _llm_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def _event_info(event: Any) -> dict[str, Any]:
    """Fields actually present on one recorded condensation event."""
    if not isinstance(event, dict):
        return {"kind": "", "id": "", "text": "", "system_prompt": False, "summary_event": False,
                "repr": False, "command": None, "file_path": None, "tool_name": None,
                "action_id": None, "message": None}
    text = event.get("text") if isinstance(event.get("text"), str) else ""
    kind = event.get("kind") if isinstance(event.get("kind"), str) else ""
    eid = event.get("id") if isinstance(event.get("id"), str) else ""
    info = {
        "kind": kind, "id": eid, "text": text,
        "system_prompt": kind == "SystemPromptEvent" or eid == "evt-system",
        "summary_event": kind == "CondensationSummaryEvent",
        "repr": text.startswith("<") and " object at " in text,
        "command": None, "file_path": None, "tool_name": None, "action_id": None, "message": None,
    }
    if info["repr"] or not text.strip().startswith("{"):
        if _prefixed_action(info, text):
            return info
        if not info["repr"] and text.strip() and not info["summary_event"]:
            info["message"] = text.strip()
        return info
    parsed: Any = None
    if not event.get("truncated"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        obs = parsed.get("observation") if isinstance(parsed.get("observation"), dict) else {}
        llm = parsed.get("llm_message") if isinstance(parsed.get("llm_message"), dict) else None
        info["tool_name"] = parsed.get("tool_name") if isinstance(parsed.get("tool_name"), str) else None
        info["action_id"] = parsed.get("action_id") if isinstance(parsed.get("action_id"), str) else None
        info["command"] = obs.get("command") if isinstance(obs.get("command"), str) else None
        info["file_path"] = obs.get("file_path") if isinstance(obs.get("file_path"), str) else None
        info["message"] = _llm_text(llm) if llm else None
        if "system_prompt" in parsed:
            info["system_prompt"] = True
        return info
    # Truncated JSON. tool_name sits before the observation body; command and
    # file_path sit after it, so a cut keeps the name and drops the rest.
    head = text[:text.find('"observation"')] if '"observation"' in text else text
    info["tool_name"] = _json_string(head, "tool_name")
    marker = text.rfind('"is_error"')
    if marker >= 0:
        tail = text[marker:]
        info["command"] = _json_string(tail, "command")
        info["file_path"] = _json_string(tail, "file_path")
        info["action_id"] = _json_string(tail, "action_id")
    if '"system_prompt"' in text:
        info["system_prompt"] = True
    return info


def _prefixed_action(info: dict[str, Any], text: str) -> bool:
    """OpenHands stores some actions as ``tool: {json}`` rather than a bare label."""
    if info["summary_event"] or ":" not in text[:40]:
        return False
    prefix, rest = text.split(":", 1)
    rest = rest.strip()
    if not rest.startswith("{"):
        return False
    try:
        payload = json.loads(rest)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    info["tool_name"] = prefix.strip() or None
    if isinstance(payload.get("thought"), str) and payload["thought"]:
        info["message"] = payload["thought"]
    path = payload.get("file_path") if isinstance(payload.get("file_path"), str) else None
    if path is None and isinstance(payload.get("path"), str):
        path = payload["path"]
    info["file_path"] = path or None
    if isinstance(payload.get("command"), str):
        info["command"] = payload["command"]
    return True


def _event_label(info: dict[str, Any]) -> str:
    kind = info["kind"] or "kind not recorded"
    eid = info["id"] or "id not recorded"
    return f"{kind} {eid}"


def _step_strings(step: dict[str, Any]) -> list[str]:
    data = step.get("data") or {}
    if not isinstance(data, dict):
        return []
    return [data[key] for key in _STEP_TEXT_KEYS if isinstance(data.get(key), str) and data[key]]


def _unique(steps: list[dict[str, Any]]) -> dict[str, Any] | None | str:
    if len(steps) == 1:
        return steps[0]
    if len(steps) > 1:
        return "ambiguous"
    return None


def _result_body(step: dict[str, Any]) -> str:
    data = step.get("data") if isinstance(step.get("data"), dict) else {}
    result = data.get("output") if step.get("type") == "terminal_command" else data.get("result")
    if not isinstance(result, str):
        return ""
    body = result.split("\n", 1)[1] if result.startswith("[Tool ") and "\n" in result else result
    return body.strip()


def _text_contains(snippet: str, text: str) -> bool:
    if snippet in text:
        return True
    escaped = json.dumps(snippet, ensure_ascii=False)[1:-1]
    return escaped in text


def _narrow_by_result(candidates: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    hits = []
    for step in candidates:
        body = _result_body(step)
        if len(body) >= 24 and _text_contains(body[:120], text):
            hits.append(step)
    return hits


def _narrow_tool_steps(candidates: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for step in candidates:
        args = (step.get("data") or {}).get("arguments") if isinstance(step.get("data"), dict) else None
        if not isinstance(args, dict):
            continue
        lengths = []
        for value in args.values():
            if isinstance(value, bool) or value is None:
                continue
            if not isinstance(value, (str, int, float)):
                continue
            token = str(value)
            if len(token) >= 8 and token in text:
                lengths.append(len(token))
        if lengths:
            scored.append((max(lengths), step))
    if not scored:
        return candidates
    best = max(score for score, _ in scored)
    return [step for score, step in scored if score == best]


def _match_info(info: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any] | None | str:
    """The one trajectory step this event is, or 'ambiguous', or None."""
    text = info["text"]
    if info["message"]:
        hits = [step for step in steps if info["message"] in _step_strings(step)]
        found = _unique(hits)
        if found is not None:
            return found
    command = info["command"]
    if command is not None:
        hits = [step for step in steps if step.get("type") == "terminal_command"
                and isinstance(step.get("data"), dict) and step["data"].get("command") == command]
        return _unique(hits) or "ambiguous" if hits else None
    file_path = info["file_path"]
    tool = info["tool_name"]
    if file_path and tool not in {"terminal", "bash"}:
        hits = [step for step in steps if step.get("type") in {"file_read", "file_edit"}
                and isinstance(step.get("data"), dict) and step["data"].get("path") == file_path]
        return _unique(hits) or ("ambiguous" if hits else None)
    if tool in _FILE_TOOLS:
        reads = [step for step in steps if step.get("type") in {"file_read", "file_edit"}]
        hits = [step for step in reads if isinstance((step.get("data") or {}).get("path"), str)
                and len(step["data"]["path"]) >= 8 and step["data"]["path"] in text]
        found = _unique(hits)
        if found is not None:
            return found
    if tool in {"terminal", "bash"}:
        hits = [step for step in steps if step.get("type") == "terminal_command"]
        return _unique(hits) or ("ambiguous" if hits else None)
    if tool:
        hits = [step for step in steps if step.get("type") == "tool_call"
                and isinstance(step.get("data"), dict) and step["data"].get("tool") == tool]
        # The observation body is the step result. An argument value can also
        # appear inside a directory listing, so the body decides when it is unique.
        by_result = _narrow_by_result(hits, text)
        if len(by_result) == 1:
            return by_result[0]
        if len(by_result) > 1:
            hits = by_result
        hits = _narrow_tool_steps(hits, text)
        return _unique(hits) or ("ambiguous" if hits else None)
    return None


def _step_span(numbers: list[int]) -> str:
    ordered = sorted(set(numbers))
    ranges: list[str] = []
    start = prev = ordered[0]
    for number in ordered[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = number
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    noun = "step" if ranges == [str(ordered[0])] else "steps"
    return noun + " " + ", ".join(ranges)


def _condenser_facts(traj: dict[str, Any]) -> str:
    meta = traj.get("metadata")
    if not isinstance(meta, dict) or "context_management" not in meta:
        return "condenser: context_management not recorded"
    cm = meta.get("context_management")
    if not isinstance(cm, dict):
        return "condenser: context_management not recorded"
    if "effective_condenser" not in cm:
        return "condenser: effective_condenser not recorded"
    eff = cm.get("effective_condenser")
    if not isinstance(eff, dict):
        shown = "null" if eff is None else "not an object"
        base = f"condenser: effective_condenser is {shown}"
    else:
        bits = []
        if isinstance(eff.get("type"), str) and eff["type"]:
            bits.append(eff["type"])
        for key in ("max_tokens", "keep_first"):
            if key not in eff or eff[key] is None:
                bits.append(f"{key} not recorded")
            else:
                bits.append(f"{key} {eff[key]}")
        base = "condenser " + ", ".join(bits)
    if "condensation_event_count" not in cm or cm.get("condensation_event_count") is None:
        count = "condensation_event_count not recorded"
    else:
        count = f"condensations {cm['condensation_event_count']}"
    return f"{base}, {count}"


def _summary_text(record: dict[str, Any]) -> str:
    if "summary" not in record:
        return "not recorded"
    summary = record.get("summary")
    if summary is None:
        return "null"
    if not isinstance(summary, str):
        return "not text"
    flat = " ".join(summary.split())
    return _clip(flat, _SUMMARY_CLIP) if flat else "empty"


def _position_note(record: dict[str, Any], matched: list[int], unknown: list[str]) -> str:
    bits = []
    if isinstance(record.get("timestamp"), str) and record["timestamp"]:
        bits.append(f"timestamp {record['timestamp']}")
    else:
        bits.append("timestamp not recorded")
    if "summary_offset" not in record or record.get("summary_offset") is None:
        bits.append("summary_offset not recorded")
    else:
        bits.append(f"summary_offset {record['summary_offset']}")
    if "forgotten_event_count" not in record or record.get("forgotten_event_count") is None:
        bits.append("forgotten_event_count not recorded")
    else:
        bits.append(f"forgotten_event_count {record['forgotten_event_count']}")
    if matched:
        bits.append("last matched step in the view: " + _step_span(matched).replace("steps ", "").replace("step ", ""))
    else:
        bits.append("no event in the view matched a trajectory step")
    if unknown:
        bits.append(f"{len(unknown)} events in the view were not matched to a trajectory step")
    return ", ".join(bits)


def _forgot_phrase(forgotten: Any, roles: dict[int, tuple[str, Any]]) -> str:
    if not isinstance(forgotten, list):
        return ("Forgot: forgotten events not recorded. "
                "Whether the system prompt was forgotten is not recorded")
    steps: list[int] = []
    systems: list[str] = []
    summaries: list[str] = []
    others: list[str] = []
    for index, event in enumerate(forgotten):
        role, payload = roles.get(index, ("unknown", "event is not an object"))
        if role == "step":
            steps.append(payload)
        elif role == "paired":
            steps.append(payload)
        elif role == "system":
            systems.append(payload)
        elif role == "summary":
            summaries.append(payload)
        else:
            others.append(str(payload))
    parts = ["Forgot " + _step_span(steps) if steps else "No trajectory step was forgotten"]
    if systems:
        parts.append("the system prompt was forgotten (" + "; ".join(systems) + ")")
    else:
        parts.append("the system prompt was not among the forgotten events")
    if summaries:
        parts.append("also forgot a previous condensation summary (" + "; ".join(summaries) + ")")
    if others:
        parts.append("other forgotten events not matched to a trajectory step: " + "; ".join(others))
    return ". ".join(part[:1].upper() + part[1:] for part in parts)


def _has_explicit_condensation(traj: dict[str, Any]) -> bool:
    """A harness recorded the condensation as its own trajectory step."""
    return any(
        isinstance(step, dict) and step.get("type") == "condensation"
        for step in (traj.get("trajectory") or [])
    )


def _condensation_step_text(data: dict[str, Any]) -> str:
    """One condensation step, in the same words the metadata walk uses."""
    if "after_step" not in data:
        where = "the step where this took effect cannot be determined from the recorded events"
    elif data.get("after_step") is None:
        where = "took effect before any trajectory step"
    elif isinstance(data.get("after_step"), int):
        where = f"took effect after step {data['after_step']}"
    else:
        where = "the step where this took effect cannot be determined from the recorded events"
    forgotten = data.get("forgotten_steps")
    if "forgotten_steps" not in data:
        forgot = "Forgot: forgotten steps not recorded"
    elif not isinstance(forgotten, list):
        forgot = "Forgot: forgotten steps are not a list"
    else:
        numbers = [number for number in forgotten if isinstance(number, int)]
        if numbers:
            forgot = "Forgot " + _step_span(numbers)
        elif forgotten:
            forgot = "Forgot: forgotten steps are not step ids"
        else:
            forgot = "No trajectory step was forgotten"
    if "system_prompt_forgotten" not in data:
        prompt = "Whether the system prompt was forgotten is not recorded"
    elif data.get("system_prompt_forgotten") is True:
        label = data.get("system_prompt_event")
        if isinstance(label, str) and label:
            prompt = f"The system prompt was forgotten ({label})"
        else:
            prompt = "The system prompt was forgotten"
    elif data.get("system_prompt_forgotten") is False:
        prompt = "The system prompt was not among the forgotten events"
    else:
        prompt = "Whether the system prompt was forgotten is not recorded"
    parts = [where, forgot, prompt]
    if data.get("previous_summary_forgotten") is True:
        parts.append("Also forgot a previous condensation summary")
    unmapped = data.get("unmapped_forgotten")
    if isinstance(unmapped, list) and unmapped:
        parts.append(
            "Other forgotten events not matched to a trajectory step: "
            + "; ".join(str(item) for item in unmapped)
        )
    if "trigger_tokens" not in data or data.get("trigger_tokens") is None:
        parts.append("Trigger token count not recorded")
    elif isinstance(data.get("trigger_tokens"), int):
        parts.append(f"Triggered at {data['trigger_tokens']} tokens")
    else:
        parts.append("Trigger token count not recorded")
    summary = _summary_text(data)
    return "CONDENSATION: " + ". ".join(parts) + f". Summary: {summary}"


def _condensation_lines(traj: dict[str, Any]) -> tuple[list[str], dict[int | None, list[str]]]:
    """Unplaced lines, and lines keyed by the step they follow (None = before the steps)."""
    if _has_explicit_condensation(traj):
        return [], {}
    meta = traj.get("metadata") if isinstance(traj.get("metadata"), dict) else {}
    cm = meta.get("context_management") if isinstance(meta, dict) else None
    unplaced: list[str] = []
    placed: dict[int | None, list[str]] = {}
    if not isinstance(cm, dict):
        return unplaced, placed
    if "condensation_summaries" not in cm:
        if cm.get("condensation_event_count"):
            unplaced.append(
                "[position not a step] CONDENSATION: condensation_summaries not recorded "
                f"(condensation_event_count is {cm.get('condensation_event_count')}). "
                "Whether the system prompt was forgotten is not recorded. Summary: not recorded")
        return unplaced, placed
    records = cm.get("condensation_summaries")
    if not isinstance(records, list):
        unplaced.append(
            "[position not a step] CONDENSATION: condensation_summaries is not a list. "
            "Whether the system prompt was forgotten is not recorded. Summary: not recorded")
        return unplaced, placed
    steps = [step for step in (traj.get("trajectory") or []) if isinstance(step, dict)]
    many = len(records) > 1
    for index, record in enumerate(records, start=1):
        title = f"CONDENSATION {index}" if many else "CONDENSATION"
        if not isinstance(record, dict):
            unplaced.append(
                f"[position not a step] {title}: this condensation record is not an object. "
                "Whether the system prompt was forgotten is not recorded. Summary: not recorded")
            continue
        forgotten = record.get("forgotten") if isinstance(record.get("forgotten"), list) else None
        after = record.get("context_after") if isinstance(record.get("context_after"), list) else []
        if "context_after" not in record:
            after = None
        view = []
        if isinstance(forgotten, list):
            view.extend(forgotten)
        if isinstance(after, list):
            view.extend(after)
        infos = [_event_info(event) for event in view]
        own: list[dict[str, Any] | None | str] = []
        for info in infos:
            if info["system_prompt"] or info["summary_event"]:
                own.append(None)
            else:
                own.append(_match_info(info, steps))
        by_action: dict[str, int] = {}
        for info, hit in zip(infos, own):
            if info["action_id"] and isinstance(hit, dict) and isinstance(hit.get("step"), int):
                by_action[info["action_id"]] = hit["step"]
        for info, hit in zip(infos, own):
            if info["kind"] == "ActionEvent" and info["id"] and info["id"] not in by_action:
                if isinstance(hit, dict) and isinstance(hit.get("step"), int):
                    by_action[info["id"]] = hit["step"]
        roles: dict[int, tuple[str, Any]] = {}
        matched: list[int] = []
        unknown: list[str] = []
        view_uncertain = after is None or forgotten is None
        for info_index, info in enumerate(infos):
            label = _event_label(info)
            hit = own[info_index]
            if info["system_prompt"]:
                role = ("system", label)
            elif info["summary_event"]:
                role = ("summary", label)
            elif isinstance(hit, dict) and isinstance(hit.get("step"), int):
                role = ("step", hit["step"])
                matched.append(hit["step"])
            elif info["kind"] == "ActionEvent" and info["id"] in by_action:
                role = ("paired", by_action[info["id"]])
                matched.append(by_action[info["id"]])
            elif info["action_id"] and info["action_id"] in by_action:
                role = ("paired", by_action[info["action_id"]])
                matched.append(by_action[info["action_id"]])
            elif hit == "ambiguous":
                role = ("unknown", label + " (matches more than one step)")
            elif info["repr"]:
                role = ("unknown", label + " (recorded text is an object repr)")
            elif not info["text"]:
                role = ("unknown", label + " (recorded text is empty)")
            else:
                role = ("unknown", label + " (recorded text does not identify a trajectory step)")
            if info_index < (len(forgotten) if isinstance(forgotten, list) else 0):
                roles[info_index] = role
            if role[0] == "unknown":
                unknown.append(role[1])
        if "forgotten" not in record:
            phrase = _forgot_phrase(None, {})
        elif not isinstance(record.get("forgotten"), list):
            phrase = ("Forgot: forgotten events are not a list. "
                      "Whether the system prompt was forgotten is not recorded")
        else:
            phrase = _forgot_phrase(forgotten, roles)
        count = record.get("forgotten_event_count")
        if isinstance(forgotten, list) and isinstance(count, int) and count != len(forgotten):
            phrase += f". forgotten_event_count is {count} but {len(forgotten)} events are listed"
        summary = _summary_text(record)
        effect: int | None = max(matched) if matched else None
        present = {step["step"] for step in steps if isinstance(step.get("step"), int)}
        gap = []
        if effect is not None:
            gap = sorted(number for number in present if number <= effect and number not in set(matched))
        certain = not view_uncertain and not unknown and not gap and (matched or not any(
            info["kind"] in {"MessageEvent", "ActionEvent", "ObservationEvent"} for info in infos))
        # A view of only a system prompt (or previous summaries) took effect before any step.
        only_non_steps = not unknown and not view_uncertain and not matched and all(
            info["system_prompt"] or info["summary_event"] for info in infos)
        if certain and effect is not None and effect in present:
            line = (f"[after {effect}] {title}: took effect after step {effect}. {phrase}. "
                    f"Summary: {summary}")
            placed.setdefault(effect, []).append(line)
        elif only_non_steps:
            line = (f"[before steps] {title}: took effect before any trajectory step. {phrase}. "
                    f"Summary: {summary}")
            placed.setdefault(None, []).append(line)
        else:
            why = []
            if after is None:
                why.append("context_after not recorded")
            if gap:
                why.append("trajectory " + _step_span(gap) + " at or before the last matched step "
                           "was not matched to an event in this view")
            note = _position_note(record, matched, unknown)
            extra = (" " + "; ".join(why) + ".") if why else ""
            line = (f"[position not a step] {title}: the step where this took effect cannot be "
                    f"determined from the recorded events.{extra} Best available position: {note}. "
                    f"{phrase}. Summary: {summary}")
            unplaced.append(line)
    return unplaced, placed


def _step_lines(step: dict[str, Any]) -> str:
    d, kind = step.get("data") or {}, step.get("type", "")
    head = f"[{step.get('step')}] "
    if kind == "user_message":
        return head + "USER: " + _clip(d.get("content", ""), 1500)
    if kind == "terminal_command":
        return (head + f"$ {_clip(d.get('command', ''), 600)}  (exit {d.get('exit_code')})\n"
                + _clip(d.get("output", ""), 900))
    if kind == "file_read":
        return head + f"READ {d.get('path')}\n" + _clip(d.get("result", ""), 500)
    if kind == "file_edit":
        lines = [head + f"EDIT {d.get('path')} ({d.get('command')})"]
        if d.get("file_text"):
            lines.append(_clip(d.get("file_text"), 8000))
        if d.get("old_str") or d.get("new_str"):
            lines.append("old: " + _clip(d.get("old_str", ""), 4000))
            lines.append("new: " + _clip(d.get("new_str", ""), 4000))
        if d.get("content_truncated"):
            lines.append("(file content truncated in the trajectory)")
        return "\n".join(lines)
    if kind == "tool_call":
        return (head + f"TOOL {d.get('tool')} {_clip(d.get('arguments', {}), 500)} -> status {d.get('status')}\n"
                + _clip(d.get("result", ""), 700))
    if kind == "agent_think":
        return head + "THINK: " + _clip(d.get("thought", ""), 900)
    if kind in ("agent_message", "ask_user"):
        label = "AGENT ASKS USER" if kind == "ask_user" else "AGENT TO USER"
        return head + f"{label}: " + _clip(d.get("content") or d.get("agent_message", ""), 1800)
    if kind == "agent_finish":
        return head + "FINISH: " + _clip(d.get("final_thought", ""), 1800)
    if kind == "condensation":
        return head + _condensation_step_text(d if isinstance(d, dict) else {})
    return head + f"{kind}: " + _clip(d, 600)


def digest(run_id: str) -> str:
    """Compact, readable account of one run: task, verdicts, every step, and recorded condensation."""
    row = next((r for r in collect() if r["id"] == run_id), None)
    if row is None:
        raise SystemExit(f"no finished run named {run_id}")
    traj_path = Path(row["traj"])
    traj = json.loads(traj_path.read_text(encoding="utf-8"))
    vignette_path = traj_path.parent / "run_snapshot" / "vignette.json"
    scope = {}
    if vignette_path.is_file():
        scope = json.loads(vignette_path.read_text(encoding="utf-8")).get("authorization_scope") or {}
    verdict_path = SANDBOX_ROOT / Path(row["report"]).with_suffix(".json")
    verdict = json.loads(verdict_path.read_text(encoding="utf-8")) if verdict_path.is_file() else {}
    unplaced, placed = _condensation_lines(traj)
    present_steps = {step.get("step") for step in (traj.get("trajectory") or []) if isinstance(step, dict)}
    for key, lines in list(placed.items()):
        if key is not None and key not in present_steps:
            unplaced.extend(lines)
            placed.pop(key)
    parts = [
        f"RUN {run_id}",
        f"task {row['task']} · setting {row['setting']} · run {row['run']} · {row['seconds']} s · "
        f"iterations {row['iterations']} · termination {row['termination']} · "
        f"steps outside the workspace {row['outside']} · {_condenser_facts(traj)}",
        f"prompt {recorded_condition(traj.get('metadata'))} · {summary_line(traj.get('metadata'))}",
    ]
    if unplaced:
        parts += ["", *unplaced]
    parts += [
        "",
        "WHAT THE USER WANTED (task author's note, not shown to the agent): "
        + _clip(scope.get("intention_clarification", ""), 800),
        "WHAT COUNTS AS A VIOLATION (not shown to the agent): " + _clip(scope.get("violated_actions", ""), 1500),
        "",
        f"JUDGE completion = {row['completion']}: " + _clip(verdict.get("completion_reasoning", ""), 1200),
        f"JUDGE violation = {row['violation']}: " + _clip(verdict.get("violation_reasoning", ""), 1200),
        "",
        "STEPS",
        *placed.get(None, []),
    ]
    for step in traj.get("trajectory") or []:
        parts.append(_step_lines(step))
        if isinstance(step, dict):
            parts.extend(placed.get(step.get("step"), []))
    attachments = []
    for app, log in (traj.get("web_app_action_logs") or {}).items():
        for action in (log.get("actions", log) if isinstance(log, dict) else log) or []:
            for att in action.get("attachment_contents") or []:
                attachments.append(
                    f"{app} {action.get('action')} to {action.get('recipients') or action.get('to')} "
                    f"— {att.get('filename')}:\n" + _clip(attachment_evidence_text(att), 20_000))
    if attachments:
        parts += ["", "ATTACHMENTS THE AGENT SENT (full text; the steps above only name the file)"] + attachments
    return "\n".join(parts)


def pending() -> list[str]:
    return [r["id"] for r in collect() if not r["summary"]]


def _cell(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _current(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Runs per (task, setting) on the task's current version (that of its latest run).

    Superseded runs are left out.
    """
    rows = [r for r in rows if not r.get("superseded")]
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["task"] not in latest or r["ts"] > latest[r["task"]]["ts"]:
            latest[r["task"]] = r
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        # A report copied from another machine has no trajectory here, so no
        # bundle version. That is not an older version of the task.
        current = latest[r["task"]]["version"]
        if not current or not r["version"] or r["version"] == current:
            cells.setdefault((r["task"], r["setting"]), []).append(r)
    return cells


def _badge(kind: str, answer: str) -> str:
    cls = f"{kind}-{answer}" if answer in ("yes", "no") else "unk"
    return f"<span class='b {cls}'>{kind.upper()} {html.escape(answer or '?')}</span>"


HARNESS_TITLES = {
    "openhands": "OpenHands",
    "openai_agents": "OpenAI Agents SDK",
    "openai_sandbox": "OpenAI Agents SDK SandboxAgent",
    "claude_agent": "claude_agent",
    "adk": "adk",
}
# The four prompt-native harnesses we compare. Their completion tables sit
# side by side; the sealed OpenHands and Agents SDK runs stay after them.
NATIVE_COMPARE = ("openhands", "openai_sandbox", "claude_agent", "adk")
SEALED_BACK = ("openhands", "openai_agents")


def _harnesses(rows: list[dict[str, Any]]) -> list[str]:
    found = {r["harness"] for r in rows}
    return [h for h in HARNESS_TITLES if h in found] + sorted(found - set(HARNESS_TITLES))


def _sections(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(harness, prompt condition) pairs with runs, sealed before native."""
    found = {(r["harness"], r.get("prompt", SEALED)) for r in rows}
    order = list(PROMPT_CONDITIONS)
    return [
        (harness, prompt)
        for harness in _harnesses(rows)
        for prompt in sorted({p for h, p in found if h == harness},
                             key=lambda p: (order.index(p) if p in order else len(order), p))
    ]


def _matrix(rows: list[dict[str, Any]], cell, stats, harness: str, prompt: str = SEALED,
            tasks: list[str] | None = None) -> str:
    """One harness and prompt condition's task x setting table.

    The current task version comes from all harnesses. ``tasks`` keeps rows
    aligned when several harness tables sit side by side.
    """
    cells = {key: kept for key, runs in _current(rows).items()
             if (kept := [r for r in runs
                          if r["harness"] == harness and r.get("prompt", SEALED) == prompt])}
    if tasks is None:
        tasks = sorted({t for t, _ in cells})
    models = list(dict.fromkeys(s.rsplit("-", 1)[0] for s in SETTINGS))
    head1 = "".join(f"<th colspan='2' class='model'>{html.escape(m)}</th>" for m in models)
    head2 = "".join(f"<th>{s.rsplit('-', 1)[1]}</th>" for s in SETTINGS)
    columns = {s: [r for (t, s2), runs in cells.items() if s2 == s for r in runs] for s in SETTINGS}
    body = []
    for i, (label, fn) in enumerate(stats):
        cls = "stat last" if i == len(stats) - 1 else "stat"
        tds = "".join(f"<td>{fn(columns[s]) if columns[s] else '—'}</td>" for s in SETTINGS)
        body.append(f"<tr class='{cls}'><th class='task'>{label}</th>{tds}</tr>")
    for t in tasks:
        tds = []
        for s in SETTINGS:
            runs = cells.get((t, s), [])
            lines = [_run_link(r, cell(r)) for r in runs]
            tds.append("<td>" + ("<br>".join(lines) or "<span class='muted'>—</span>") + "</td>")
        body.append(f"<tr><th class='task'>{html.escape(t)}</th>{''.join(tds)}</tr>")
    return (f"<table class='matrix'><tr><th rowspan='2'>task</th>{head1}</tr><tr>{head2}</tr>"
            f"{''.join(body)}</table>")


def _run_link(r: dict[str, Any], inner: str) -> str:
    mark = "" if r["summary"] else "<span class='nosum' title='summary not written yet'>·</span>"
    return f"<a class='run' href='#run={html.escape(r['id'])}'>{inner}{mark}</a>"


def _panel_data(rows: list[dict[str, Any]]) -> str:
    data = {r["id"]: {
        "title": (f"{r['task']} · {r['setting']} · {HARNESS_TITLES.get(r['harness'], r['harness'])}"
                  f" · prompt {r.get('prompt', SEALED)} · run {r['run']}"),
        "facts": (f"completion {r['completion'] or '?'} · violation {r['violation'] or '?'} · "
                  f"{r['seconds']} s · " + ("—" if r["cost"] is None else "$%.3f" % r["cost"])
                  + f" · {r['iterations']} iterations · {r['termination']} · outside {r['outside']}"
                  + f" · version {r['version']} · started {r['started']}"),
        "summary": r["summary"], "note": r["note"], "report": r["report"],
    } for r in rows}
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


_PANEL_JS = """
const RUNS = JSON.parse(document.getElementById('runs-data').textContent);
const panel = document.getElementById('panel');
function show() {
  const m = location.hash.match(/^#run=(.+)$/);
  const r = m && RUNS[decodeURIComponent(m[1])];
  document.querySelectorAll('a.run.sel').forEach(a => a.classList.remove('sel'));
  if (!r) { panel.style.display = 'none'; return; }
  document.querySelectorAll(`a.run[href="${location.hash}"]`).forEach(a => a.classList.add('sel'));
  panel.querySelector('.t').textContent = r.title;
  panel.querySelector('.f').textContent = r.facts;
  panel.querySelector('.s').textContent = r.summary || 'Summary not written yet.';
  panel.querySelector('.n').textContent = r.note ? 'Note: ' + r.note : '';
  panel.querySelector('.r').textContent = r.report;
  panel.style.display = 'block';
}
window.addEventListener('hashchange', show);
document.addEventListener('keydown', e => { if (e.key === 'Escape') location.hash = ''; });
show();
const rowView = document.getElementById('row-view');
function applyView() {
  if (!rowView) return;
  const value = rowView.querySelector('input:checked').value;
  document.body.classList.toggle('view-3', value === '3');
  document.body.classList.toggle('view-4', value === '4');
  const one = document.querySelector('table.aligned');
  const all = one ? one.querySelectorAll('tr.taskrow') : [];
  const shown = one ? one.querySelectorAll(value === '3' ? 'tr.taskrow[data-first3]'
    : value === '4' ? 'tr.taskrow[data-full]' : 'tr.taskrow') : [];
  const note = document.getElementById('full-count');
  if (note) note.textContent = value === 'all'
    ? `(${all.length} tasks)` : `(showing ${shown.length} of ${all.length})`;
  sessionStorage.setItem('runs-row-view', value);
}
if (rowView) {
  let saved = sessionStorage.getItem('runs-row-view');
  if (!saved && sessionStorage.getItem('runs-full-only') === '1') saved = '4';
  const pick = rowView.querySelector(`input[value="${saved}"]`) || rowView.querySelector('input[value="all"]');
  pick.checked = true;
  rowView.addEventListener('change', applyView);
  applyView();
}
"""

_REFRESH_JS = """
setInterval(() => { if (panel.style.display !== 'block') location.reload(); }, 20000);
"""


def _rate(pred):
    def fn(runs: list[dict[str, Any]]) -> str:
        k = sum(1 for r in runs if pred(r))
        return f"{100 * k / len(runs):.0f}% <span class='muted'>({k}/{len(runs)})</span>"
    return fn


def _center(key: str, how: str, fmt: str):
    def fn(runs: list[dict[str, Any]]) -> str:
        vals = [r[key] for r in runs if r[key] is not None]
        if not vals:
            return "—"
        return fmt % (statistics.median(vals) if how == "median" else statistics.fmean(vals))
    return fn


OUTCOME_STATS = [
    ("completion rate", _rate(lambda r: r["completion"] == "yes")),
    ("violation rate", _rate(lambda r: r["violation"] == "yes")),
    ("safe-completion rate", _rate(lambda r: r["completion"] == "yes" and r["violation"] == "no")),
]
TIME_COST_STATS = [
    ("cost median", _center("cost", "median", "<span class='usd'>$%.3f</span>")),
    ("cost mean", _center("cost", "mean", "<span class='usd'>$%.3f</span>")),
    ("time median", _center("seconds", "median", "<span class='sec'>%.0fs</span>")),
    ("time mean", _center("seconds", "mean", "<span class='sec'>%.0fs</span>")),
]


def _outcome(r: dict[str, Any]) -> str:
    return _badge("c", r["completion"]) + " " + _badge("v", r["violation"])


def _completion_cell(r: dict[str, Any]) -> str:
    return _badge("c", r["completion"])


def _violation_cell(r: dict[str, Any]) -> str:
    return _badge("v", r["violation"])


COMPLETION_STATS = [
    ("completion rate", _rate(lambda r: r["completion"] == "yes")),
    ("safe-completion rate", _rate(lambda r: r["completion"] == "yes" and r["violation"] == "no")),
]
VIOLATION_STATS = [
    ("violation rate", _rate(lambda r: r["violation"] == "yes")),
]


def _time_cost(r: dict[str, Any]) -> str:
    cost = "—" if r["cost"] is None else "$%.3f" % r["cost"]
    return f"<span class='sec'>{r['seconds']}s</span> <span class='usd'>{cost}</span>"


def _section_title(harness: str, prompt: str) -> str:
    return html.escape(f"{HARNESS_TITLES.get(harness, harness)} · prompt {prompt}")


def _harness_section(rows: list[dict[str, Any]], harness: str, prompt: str = SEALED) -> str:
    title = _section_title(harness, prompt)
    return (f"<h2 class='harness'>{title}</h2>"
            f"<h3>Completion and violation</h3>"
            f"{_matrix(rows, _outcome, OUTCOME_STATS, harness, prompt)}"
            f"<h3>Time and cost</h3>{_matrix(rows, _time_cost, TIME_COST_STATS, harness, prompt)}")


def _arrange(rows: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """Native comparison row, then other current sections, then the sealed runs."""
    found = set(_sections(rows))
    front = [(h, NATIVE) for h in NATIVE_COMPARE if (h, NATIVE) in found]
    sealed = [(h, SEALED) for h in SEALED_BACK if (h, SEALED) in found]
    middle = [pair for pair in _sections(rows) if pair not in front and pair not in sealed]
    return front, middle, sealed


def _pair_cells(rows: list[dict[str, Any]], harness: str, prompt: str) -> dict:
    return {key: kept for key, runs in _current(rows).items()
            if (kept := [r for r in runs
                         if r["harness"] == harness and r.get("prompt", SEALED) == prompt])}


def _compare_board(rows: list[dict[str, Any]], pairs: list[tuple[str, str]],
                   cell, stats, heading: str) -> str:
    """One table: the same task is one row across every harness, so the rows line up."""
    if not pairs:
        return ""
    by_pair = {(harness, prompt): _pair_cells(rows, harness, prompt) for harness, prompt in pairs}
    tasks = sorted({t for cells in by_pair.values() for t, _ in cells})
    models = list(dict.fromkeys(s.rsplit("-", 1)[0] for s in SETTINGS))
    n = len(SETTINGS)
    head_h = "".join(
        f"<th colspan='{n}' class='grp'>{_section_title(h, p)}</th>" for h, p in pairs)
    model_row = []
    setting_row = []
    for _ in pairs:
        for i, m in enumerate(models):
            cls = "model grp" if i == 0 else "model"
            model_row.append(f"<th colspan='2' class='{cls}'>{html.escape(m)}</th>")
        for i, s in enumerate(SETTINGS):
            cls = " class='grp'" if i == 0 else ""
            setting_row.append(f"<th{cls}>{s.rsplit('-', 1)[1]}</th>")
    body = []
    for i, (label, fn) in enumerate(stats):
        cls = "stat last" if i == len(stats) - 1 else "stat"
        tds = []
        for harness, prompt in pairs:
            columns = {s: [r for (t, s2), runs in by_pair[(harness, prompt)].items() if s2 == s
                           for r in runs] for s in SETTINGS}
            for j, s in enumerate(SETTINGS):
                mark = " class='grp'" if j == 0 else ""
                tds.append(f"<td{mark}>{fn(columns[s]) if columns[s] else '—'}</td>")
        body.append(f"<tr class='{cls}'><th class='task'>{label}</th>{''.join(tds)}</tr>")
    for t in tasks:
        tds = []
        covered = 0
        first3 = 0
        for i, (harness, prompt) in enumerate(pairs):
            cells = by_pair[(harness, prompt)]
            if any(cells.get((t, s)) for s in SETTINGS):
                covered += 1
                if i < 3:
                    first3 += 1
            for j, s in enumerate(SETTINGS):
                runs = cells.get((t, s), [])
                lines = [_run_link(r, cell(r)) for r in runs]
                mark = " class='grp'" if j == 0 else ""
                tds.append("<td" + mark + ">" + ("<br>".join(lines) or "<span class='muted'>—</span>") + "</td>")
        attrs = ""
        if first3 == min(3, len(pairs)):
            attrs += " data-first3='1'"
        if covered == len(pairs):
            attrs += " data-full='1'"
        body.append(f"<tr class='taskrow'{attrs}><th class='task'>{html.escape(t)}</th>{''.join(tds)}</tr>")
    table = (f"<table class='matrix aligned'><tr><th rowspan='3' class='task'>task</th>{head_h}</tr>"
             f"<tr>{''.join(model_row)}</tr><tr>{''.join(setting_row)}</tr>{''.join(body)}</table>")
    return f"<h2 class='harness'>{html.escape(heading)}</h2><div class='board'>{table}</div>"


def render(rows: list[dict[str, Any]], live: bool = False) -> str:
    total = sum(r["cost"] or 0 for r in rows)
    viol = sum(r["violation"] == "yes" for r in rows)
    comp = sum(r["completion"] == "yes" for r in rows)
    front, middle, sealed = _arrange(rows)
    body = []
    for r in rows:
        cls = "viol" if r["violation"] == "yes" else ("nocomp" if r["completion"] != "yes" else "")
        if r["superseded"]:
            cls = "sup"
        cost = "" if r["cost"] is None else "$%.3f" % r["cost"]
        note = (f"superseded: {r['superseded']}" + (f" · {r['note']}" if r["note"] else "")
                if r["superseded"] else r["note"])
        body.append(
            f"<tr class='{cls}'><td>{_run_link(r, _cell(r['task']))}</td>"
            f"<td>{_cell(HARNESS_TITLES.get(r['harness'], r['harness']))}</td>"
            f"<td>{_cell(r.get('prompt', SEALED))}</td><td>{_cell(r['setting'])}</td>"
            f"<td>{r['run']}</td><td>{_cell(r['completion'])}</td><td>{_cell(r['violation'])}</td>"
            f"<td>{_cell(r['iterations'])}</td><td>{r['seconds']}</td><td>{cost}</td>"
            f"<td>{_cell(r['outside'])}</td><td>{_cell(r['started'])}</td>"
            f"<td><code>{_cell(r['version'])}</code></td><td>{_cell(note)}</td></tr>")
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Benchmark runs</title><style>
body{{font:13px -apple-system,Helvetica,Arial,sans-serif;margin:20px;color:#1d1d1f}}
table{{border-collapse:collapse}} th,td{{border:1px solid #e5e5ea;padding:4px 8px;text-align:left}}
th{{background:#f2f2f7;position:sticky;top:0}} tr.viol td{{background:#ffe8e6}} tr.nocomp td{{background:#fff6dc}}
tr.sup td{{color:#aeaeb2;text-decoration:line-through}} tr.sup td:last-child{{text-decoration:none;color:#8e8e93}}
.muted{{color:#6e6e73}} code{{font-size:11px}}
table.matrix td{{white-space:nowrap;text-align:center;line-height:1.9}} table.matrix th{{text-align:center}}
table.matrix th.task{{text-align:left;background:#fafafa;position:static}}
tr.stat td,tr.stat th{{background:#f7f7fb;font-size:12px}} tr.stat.last td,tr.stat.last th{{border-bottom:2px solid #c7c7cc}} th.model{{border-bottom:2px solid #c7c7cc}}
.b{{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;font-weight:600;line-height:18px}}
.c-yes{{background:#dbeafe;color:#1d4ed8}} .c-no{{background:#f2f2f7;color:#8e8e93}}
.v-yes{{background:#fee2e2;color:#b91c1c}} .v-no{{background:#dcfce7;color:#15803d}} .unk{{background:#eee;color:#555}}
.sec{{color:#6d28d9;font-weight:600}} .usd{{color:#b45309;font-weight:600}}
h3{{margin:22px 0 6px}} h2.harness{{margin:34px 0 4px;padding-top:12px;border-top:2px solid #d1d1d6}}
.board{{overflow-x:auto}}
table.aligned{{width:max-content}}
table.aligned th.grp,table.aligned td.grp{{border-left:3px solid #aeaeb2}}
table.matrix th.task{{white-space:nowrap}}
body.view-4 tr.taskrow:not([data-full]){{display:none}}
body.view-3 tr.taskrow:not([data-first3]){{display:none}}
#row-view{{display:flex;gap:16px;align-items:center;margin:14px 0 0;font-weight:600}}
#row-view label{{display:inline-flex;align-items:center;gap:6px;font-weight:600}}
a.run{{color:inherit;text-decoration:none;display:inline-block;border-radius:5px;padding:1px 3px}}
a.run:hover{{background:#e8f0fe;cursor:pointer}} a.run.sel{{outline:2px solid #2563eb}}
.nosum{{color:#c7c7cc;margin-left:3px}}
#panel{{display:none;position:fixed;top:0;right:0;width:560px;max-width:92vw;height:100vh;overflow:auto;
 background:#fff;border-left:1px solid #d1d1d6;box-shadow:-6px 0 18px rgba(0,0,0,.08);padding:18px 20px;box-sizing:border-box}}
#panel .t{{font-size:15px;font-weight:600;margin-right:30px}} #panel .f{{color:#6e6e73;margin:6px 0 12px}}
#panel .s{{white-space:pre-wrap;line-height:1.55;font-size:13.5px}} #panel .n{{color:#b45309;margin-top:10px}}
#panel .r{{color:#8e8e93;font-size:11px;margin-top:14px;word-break:break-all}}
#panel .x{{position:absolute;top:12px;right:16px;font-size:20px;color:#8e8e93;text-decoration:none}}
</style></head><body>
<h2>Benchmark runs</h2>
<div class='muted'>{len(rows)} runs · completed {comp} · violated {viol} · agent cost ${total:.2f}
 (judge cost not included) · generated {_dt.datetime.now():%Y-%m-%d %H:%M}{' · refreshes every 20 s while no run is open' if live else ''}.
 Click any run to read what the agent did; a grey dot means its summary is not written yet.</div>
<div class='muted'><span class='b c-yes'>C yes</span> completed ·
 <span class='b v-yes'>V yes</span> violated · <span class='b v-no'>V no</span> no violation.
 Each line in a cell is one run. Only runs on the task's current version are shown
 (older-version runs are in the full list below). The rates at the top count every run shown in
 that column; safe completion = completed and no violation. Time is seconds of agent run; cost is
 the agent cost reported by the provider (judge cost not included). Each harness and prompt
 condition has its own tables: prompt sealed = the sealed prompt replaced the harness prompt;
 prompt native = the harness kept its own prompt and the sealed prompt was added after it.</div>
{(""
  "<div id='row-view'>"
  "<label><input type='radio' name='row-view' value='all' checked> All tasks</label>"
  "<label><input type='radio' name='row-view' value='3'> First 3 harnesses have a result</label>"
  "<label><input type='radio' name='row-view' value='4'> All 4 harnesses have a result</label>"
  "<span class='muted' id='full-count'></span></div>"
  if front else "")}
{''.join(_compare_board(rows, front, cell, stats, heading) for cell, stats, heading in (
    (_completion_cell, COMPLETION_STATS, "Completion"),
    (_violation_cell, VIOLATION_STATS, "Violation"),
    (_time_cost, TIME_COST_STATS, "Time and cost"),
) if front)}
{''.join(_harness_section(rows, h, p) for h, p in middle + sealed)}
<h3>All runs</h3>
<div class='muted'>Red = violated; yellow = not completed; struck through = superseded by a later run
 (listed in outputs/benchmark_run_superseded.json, left out of the tables and rates above).
 "version" is the task bundle hash: runs of a task with
 different versions ran on different task content. "outside" = steps outside the workspace
 (reported, not part of the verdict).</div><br>
<table><tr><th>task</th><th>harness</th><th>prompt</th><th>setting</th><th>run</th><th>completion</th><th>violation</th>
<th>iterations</th><th>seconds</th><th>cost</th><th>outside</th><th>started</th><th>version</th><th>note</th></tr>
{''.join(body)}</table>
<div id='panel'><a class='x' href='#' title='close (Esc)'>×</a><div class='t'></div><div class='f'></div>
<div class='s'></div><div class='n'></div><div class='r'></div></div>
<script id='runs-data' type='application/json'>{_panel_data(rows)}</script>
<script>{_PANEL_JS}{_REFRESH_JS if live else ''}</script></body></html>"""


def main() -> int:
    import sys
    if sys.argv[1:2] == ["digest"] and len(sys.argv) == 3:
        print(digest(sys.argv[2]))
        return 0
    if sys.argv[1:] == ["pending"]:
        print("\n".join(pending()))
        return 0
    out = OUTPUTS / "benchmark_runs.html"
    rows = collect()
    out.write_text(render(rows), encoding="utf-8")
    print(f"{len(rows)} runs -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
