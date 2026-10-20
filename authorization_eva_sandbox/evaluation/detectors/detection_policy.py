"""
detection_policy.py — task-level policy/method resolution for FS evidence.

Canonical shape:
    task_config["detection_policy"] = {
        "methods": ["llm", "hash_diff", "trajectory_semantics"],
        "allowlist": [...],
        "denylist": [...],
    }

Notes:
    - methods are additive; evaluators can combine evidence channels.
    - default is ["llm"] when not specified.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DETECTION_METHODS = {"llm", "hash_diff", "trajectory_semantics"}


@dataclass
class DetectionPolicy:
    methods: list[str]
    allowlist: list[str]
    denylist: list[str]


def _norm_path_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
    return sorted(set(out))


def _normalize_methods(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["llm"]
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if not s:
            continue
        if s in DETECTION_METHODS:
            out.append(s)
    out = sorted(set(out))
    return out or ["llm"]


def resolve_detection_policy(task_config: dict | None) -> DetectionPolicy:
    cfg = task_config or {}

    raw = cfg.get("detection_policy")
    if isinstance(raw, dict):
        methods = _normalize_methods(raw.get("methods"))
        allow = _norm_path_list(raw.get("allowlist"))
        deny = _norm_path_list(raw.get("denylist"))
        return DetectionPolicy(methods=methods, allowlist=allow, denylist=deny)

    return DetectionPolicy(methods=["llm"], allowlist=[], denylist=[])
