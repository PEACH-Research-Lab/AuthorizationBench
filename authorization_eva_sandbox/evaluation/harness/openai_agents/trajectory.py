"""Insert each condensation into the normalized trajectory where it took effect.

``record_openhands_events`` skips ``Condensation``. This walks the same events,
maps forgotten event ids onto the trajectory steps those events became, and
splices one ``condensation`` step after the last step that already existed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.trajectory_logger import TrajectoryLog, TrajectoryStep


@dataclass
class _Insertion:
    after_original: int
    forgotten_original: list[int]
    summary: str
    trigger_tokens: int | None
    system_prompt_forgotten: bool
    system_prompt_event: str | None
    previous_summary_forgotten: bool
    unmapped_forgotten: list[str] = field(default_factory=list)


@dataclass
class _Plan:
    slot_count: int
    insertions: list[_Insertion]


def place_condensation_steps(trajectory: TrajectoryLog, events: list[Any]) -> None:
    """Splice condensation steps into a log ``record_openhands_events`` just filled."""
    raw = [
        {"step": step.step, "timestamp": step.timestamp, "type": step.type, "data": step.data}
        for step in trajectory.steps
    ]
    spliced = splice_condensation_steps(raw, events)
    trajectory.steps = [
        TrajectoryStep(
            step=item["step"],
            timestamp=item["timestamp"],
            type=item["type"],
            data=item["data"],
        )
        for item in spliced
    ]


def record_condensation_in_trajectory_file(outputs_path: str | Path, events: list[Any]) -> None:
    """Patch ``traj_*.json`` written by ``run_single_task``. Metadata is left as it is."""
    folder = Path(outputs_path)
    if not folder.is_dir():
        return
    matches = sorted(folder.glob("traj_*.json"))
    if len(matches) != 1:
        return
    path = matches[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data.get("trajectory")
    if not isinstance(steps, list):
        return
    data["trajectory"] = splice_condensation_steps(steps, events)
    data["total_steps"] = len(data["trajectory"])
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def splice_condensation_steps(steps: list[dict[str, Any]], events: list[Any]) -> list[dict[str, Any]]:
    """Return ``steps`` with one condensation dict inserted per condensation event."""
    plan = _plan(events)
    if not plan.insertions:
        return steps
    if len(steps) != plan.slot_count:
        raise RuntimeError(
            "condensation placement saw "
            f"{plan.slot_count} steps in the events and {len(steps)} recorded steps"
        )
    buckets: list[list[_Insertion]] = [[] for _ in range(len(steps) + 1)]
    for insertion in plan.insertions:
        buckets[insertion.after_original].append(insertion)
    merged: list[dict[str, Any]] = []
    for index in range(len(steps) + 1):
        for insertion in buckets[index]:
            merged.append(_condensation_dict(insertion, plan.insertions))
        if index < len(steps):
            merged.append(dict(steps[index]))
    for number, step in enumerate(merged, start=1):
        step["step"] = number
    return merged


def _condensation_dict(insertion: _Insertion, insertions: list[_Insertion]) -> dict[str, Any]:
    after = None if insertion.after_original == 0 else _shift(insertion.after_original, insertions)
    forgotten = sorted({
        _shift(number, insertions) for number in insertion.forgotten_original
    })
    return {
        "step": 0,
        "timestamp": time.time(),
        "type": "condensation",
        "data": {
            "summary": insertion.summary,
            "after_step": after,
            "forgotten_steps": forgotten,
            "system_prompt_forgotten": insertion.system_prompt_forgotten,
            "system_prompt_event": insertion.system_prompt_event,
            "previous_summary_forgotten": insertion.previous_summary_forgotten,
            "trigger_tokens": insertion.trigger_tokens,
            "unmapped_forgotten": list(insertion.unmapped_forgotten),
        },
    }


def _shift(original: int, insertions: list[_Insertion]) -> int:
    """Step number after earlier condensation steps have been inserted."""
    return original + sum(1 for item in insertions if item.after_original < original)


def _plan(events: list[Any]) -> _Plan:
    from authorization_eva_sandbox.evaluation.harness.openhands_events import (
        _event_kind,
        _observation_text,
    )

    if not any(_event_kind(event) == "Condensation" for event in events):
        return _Plan(0, [])

    items: list[tuple[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    marks: list[tuple[int, Any]] = []
    for event in events:
        kind = _event_kind(event)
        if kind == "Condensation":
            marks.append((len(items), event))
            continue
        if kind == "MessageEvent":
            message = getattr(event, "llm_message", None)
            content = getattr(message, "content", None) or []
            texts = [getattr(block, "text", "") for block in content]
            text = "\n".join(part for part in texts if part).strip()
            if text:
                items.append(("message", event))
            continue
        if kind == "ObservationEvent":
            entry = None
            for key in (getattr(event, "action_id", None), getattr(event, "tool_call_id", None)):
                if isinstance(key, str) and key in by_id and by_id[key]["observation"] is None:
                    entry = by_id[key]
                    break
            if entry is None:
                observed = str(getattr(event, "tool_name", "") or "")
                entry = next(
                    (
                        item for kind_, item in items
                        if kind_ == "action" and item["observation"] is None
                        and item["tool"] != "finish"
                        and (not observed or item["tool"] == observed)
                    ),
                    None,
                )
            if entry is not None:
                entry["observation"] = event
            continue
        if kind != "ActionEvent":
            continue
        entry = {
            "tool": str(getattr(event, "tool_name", "") or ""),
            "observation": None,
            "event": event,
        }
        items.append(("action", entry))
        for key in (getattr(event, "id", None), getattr(event, "tool_call_id", None)):
            if isinstance(key, str) and key:
                by_id[key] = entry

    id_to_step: dict[str, int] = {}
    step_no = 0
    item_steps: list[int] = []
    for kind, item in items:
        if kind == "message":
            step_no += 1
            _remember(id_to_step, getattr(item, "id", None), step_no)
        else:
            step_no += 1
            action_step = step_no
            _remember(id_to_step, getattr(item["event"], "id", None), action_step)
            observation = item["observation"]
            obs_step = action_step
            if item["tool"] in {"run_ipython", "ipython"} and _observation_text(observation):
                step_no += 1
                obs_step = step_no
            if observation is not None:
                _remember(id_to_step, getattr(observation, "id", None), obs_step)
        item_steps.append(step_no)

    kind_of: dict[str, str] = {}
    system_labels: dict[str, str] = {}
    for event in events:
        eid = str(getattr(event, "id", "") or "")
        if not eid:
            continue
        kind_of[eid] = type(event).__name__
        if type(event).__name__ == "SystemPromptEvent":
            system_labels[eid] = f"SystemPromptEvent {eid}"

    insertions: list[_Insertion] = []
    for after_items, event in marks:
        after_original = item_steps[after_items - 1] if after_items else 0
        forgotten_ids = [str(item) for item in (getattr(event, "forgotten_event_ids", None) or [])]
        forgotten_steps: list[int] = []
        unmapped: list[str] = []
        seen: set[str] = set()
        for eid in forgotten_ids:
            if not eid or eid in seen:
                continue
            seen.add(eid)
            if eid in system_labels:
                continue
            step = id_to_step.get(eid)
            if step is None:
                unmapped.append(f"{kind_of.get(eid, 'kind not recorded')} {eid}")
            elif step not in forgotten_steps:
                forgotten_steps.append(step)
        labels = [system_labels[eid] for eid in forgotten_ids if eid in system_labels]
        trigger = getattr(event, "trigger_tokens", None)
        insertions.append(_Insertion(
            after_original=after_original,
            forgotten_original=forgotten_steps,
            summary=str(getattr(event, "summary", "") or ""),
            trigger_tokens=trigger if isinstance(trigger, int) else None,
            system_prompt_forgotten=bool(labels),
            system_prompt_event="; ".join(dict.fromkeys(labels)) or None,
            previous_summary_forgotten=bool(getattr(event, "previous_summary_forgotten", False)),
            unmapped_forgotten=unmapped,
        ))
    return _Plan(step_no, insertions)


def _remember(id_to_step: dict[str, int], eid: Any, step: int) -> None:
    if isinstance(eid, str) and eid and eid not in id_to_step:
        id_to_step[eid] = step
