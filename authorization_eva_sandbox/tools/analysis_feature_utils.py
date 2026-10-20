#!/usr/bin/env python3
"""Shared helpers for analysis feature extraction/reporting."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

TURN_LEVEL_FIELDS = [
    "turns_total",
    "user_message_count",
    "agent_think_count",
    "agent_think_tokens",
    "turn_metrics_status",
    "turn_metrics_error",
]

COST_LEVEL_FIELDS = [
    # Server-reported per-run reasoning token count (subset of completion_tokens).
    # 0 for non-reasoning models; positive for gpt-5.x, claude-sonnet (extended
    # thinking), gemini-3.x, qwen-thinking, etc. See reasoning_token_count.md.
    "reasoning_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cost_metrics_status",
]


@lru_cache(maxsize=1)
def _get_cl100k_encoder():
    try:
        import tiktoken
    except Exception as exc:  # pragma: no cover - surfaced via status field
        raise RuntimeError("tiktoken is required for cl100k_base token counts") from exc
    return tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _get_cl100k_encoder()
    return len(enc.encode(text))


def _blank_metrics(status: str, error: str = "") -> dict[str, Any]:
    return {
        "turns_total": "",
        "user_message_count": "",
        "agent_think_count": "",
        "agent_think_tokens": "",
        "turn_metrics_status": status,
        "turn_metrics_error": error,
    }


def compute_turn_metrics_from_traj(traj_path: str) -> dict[str, Any]:
    """Compute turn-level metrics from traj_*.json path."""
    if not traj_path:
        return _blank_metrics("missing_traj_path")

    p = Path(traj_path)
    if not p.exists():
        return _blank_metrics("traj_not_found", str(p))

    try:
        traj = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return _blank_metrics("traj_parse_error", str(exc))

    steps = traj.get("trajectory")
    if not isinstance(steps, list):
        steps = []

    turns_total = len(steps)
    if turns_total == 0:
        ts = traj.get("total_steps")
        if isinstance(ts, int):
            turns_total = ts

    user_message_count = 0
    agent_think_count = 0
    agent_think_tokens = 0
    token_fallback_error = ""

    for step in steps:
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        data = step.get("data") or {}
        if stype == "user_message":
            user_message_count += 1
        if stype == "agent_think":
            agent_think_count += 1
            if isinstance(data, dict):
                thought = data.get("thought") or data.get("content") or ""
            else:
                thought = ""
            try:
                agent_think_tokens += _count_tokens(str(thought))
            except Exception as exc:
                token_fallback_error = str(exc)
                # Fallback proxy when cl100k tokenizer cannot be loaded offline.
                agent_think_tokens += len(str(thought).split())

    status = "ok"
    err = ""
    if token_fallback_error:
        status = "tokenizer_fallback_whitespace"
        err = token_fallback_error
    return {
        "turns_total": turns_total,
        "user_message_count": user_message_count,
        "agent_think_count": agent_think_count,
        "agent_think_tokens": agent_think_tokens,
        "turn_metrics_status": status,
        "turn_metrics_error": err,
    }


def resolve_output_file_to_abs(output_file: str, repo_root: Path) -> str:
    """Resolve CSV output_file path to absolute traj path."""
    if not output_file:
        return ""
    p = Path(output_file)
    if p.is_absolute():
        return str(p)
    return str((repo_root / p).resolve())


def _blank_cost_metrics(status: str) -> dict[str, Any]:
    return {
        "reasoning_tokens": "",
        "prompt_tokens": "",
        "completion_tokens": "",
        "cost_metrics_status": status,
    }


def derive_cost_path(traj_path: str) -> str:
    """Given an absolute traj_<task>.json path, return the sibling cost_<task>.json.

    traj filename pattern: traj_<task>.json. cost filename mirrors the suffix.
    Returns "" if the input does not match the expected pattern.
    """
    if not traj_path:
        return ""
    p = Path(traj_path)
    name = p.name
    if not (name.startswith("traj_") and name.endswith(".json")):
        return ""
    cost_name = "cost_" + name[len("traj_"):]
    return str(p.with_name(cost_name))


def compute_cost_metrics_from_path(cost_path: str) -> dict[str, Any]:
    """Read cost_<task>.json and return per-run reasoning / token totals.

    Pulls from `combined.{reasoning_tokens, prompt_tokens, completion_tokens}`
    written by run_eval.py. Missing keys (older runs from before the patch) are
    surfaced as empty strings + a status field, so analysis can distinguish
    "0 reasoning tokens" from "this run pre-dates the patch".
    """
    if not cost_path:
        return _blank_cost_metrics("missing_cost_path")
    p = Path(cost_path)
    if not p.exists():
        return _blank_cost_metrics("cost_not_found")
    try:
        cost = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        d = _blank_cost_metrics("cost_parse_error")
        d["cost_metrics_status"] = f"cost_parse_error:{exc}"
        return d

    combined = cost.get("combined") or {}
    rt = combined.get("reasoning_tokens")
    pt = combined.get("prompt_tokens")
    ct = combined.get("completion_tokens")

    if rt is None and pt is None and ct is None:
        # Pre-patch run; cost file exists but does not record these fields.
        return _blank_cost_metrics("pre_patch_no_combined")

    return {
        "reasoning_tokens": rt if rt is not None else 0,
        "prompt_tokens": pt if pt is not None else "",
        "completion_tokens": ct if ct is not None else "",
        "cost_metrics_status": "ok",
    }
