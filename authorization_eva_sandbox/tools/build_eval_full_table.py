#!/usr/bin/env python3
"""Build a complete per-run analysis table from on-disk post-hoc reports.

Scans evaluation/detectors/post_hoc_eval_report/*/*.json (one row per report).
A later harness adds a directory: post_hoc_eval_report/<harness>/<setting>/*.json.
Does not use batch_task_first.jsonl — all 84×6 pairs come from post-hoc JSON.

Example:
  ./au312/bin/python authorization_eva_sandbox/tools/build_eval_full_table.py \\
    --output authorization_eva_sandbox/evaluation/outputs/eval_full_504.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from batch_run_eval import infer_repo_root, load_llm_profile_tags  # noqa: E402

MODEL_DISPLAY = {
    "gpt5_5": "gpt5.5",
    "gpt4_1": "gpt4.1",
    "sonnet": "sonnet4.6",
    "gemini_3_1_pro": "gemini 3.1 pro",
    "qwen3_vl_30b_a3b_instruct": "qwen3-instruct",
    "qwen3_vl_30b_a3b_thinking": "qwen3-thinking",
}

CSV_COLUMNS = [
    "task",
    "agent_profile",
    "model_tag",
    "model_display",
    "output_file",
    "report_file",
    "domain",
    "harm",
    "situational_trigger",
    "completion",
    "violation",
    "safe_completion",
    "duration_seconds",
    "agent_iterations",
    "agent_iterations_max",
    "agent_user_messages",
    "termination_reason",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "reasoning_share",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd_table",
    "cost_usd_provider",
    "cost_priced_by_table",
    "total_steps",
    "total_webapp_actions",
]


def _task_meta(
    task_id: str,
    *,
    tasks_root: Path,
    repo_root: Path,
    cache: dict[str, dict[str, str]],
) -> dict[str, str]:
    if task_id in cache:
        return cache[task_id]

    out = {"domain": "", "harm": "", "situational_trigger": ""}
    cfg_path = tasks_root / task_id / "task_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cache[task_id] = out
        return out

    vignette_id = cfg.get("source_vignette")
    if not isinstance(vignette_id, str) or not vignette_id:
        cache[task_id] = out
        return out

    vignette_path = repo_root / "authorization_eva_sandbox" / "vignettes" / f"{vignette_id}.json"
    try:
        vig = json.loads(vignette_path.read_text(encoding="utf-8"))
    except Exception:
        cache[task_id] = out
        return out

    out["domain"] = str(vig.get("domain", "") or "")
    out["harm"] = str(vig.get("harm", "") or "")
    st = vig.get("situational_trigger", "")
    if isinstance(st, list):
        out["situational_trigger"] = " | ".join(str(x) for x in st)
    else:
        out["situational_trigger"] = str(st or "")

    cache[task_id] = out
    return out


def _to_rel(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _resolve_traj(traj_path_raw: str, evaluation_root: Path) -> Path:
    if not traj_path_raw:
        return Path()
    p = Path(traj_path_raw)
    if p.is_absolute():
        return p
    # post-hoc stores paths like outputs/<model>/... relative to evaluation/
    return (evaluation_root / p).resolve()


def _invert_profile_tags(profile_tags: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for profile, tag in profile_tags.items():
        out[tag] = profile
    return out



def _parse_report_filename(
    report_path: Path,
    model_tag: str,
    harness: str | None = None,
) -> tuple[str, int] | None:
    """Parse task id + run index from output_full_{task}_{model_tag}_{n}.json.

    A non-OpenHands report is output_full_{task}_{model_tag}_{harness}_{n}.json.
    """
    stem = report_path.stem
    if harness:
        suffix = f"_{model_tag}_{harness}_"
    else:
        suffix = f"_{model_tag}_"
    if not stem.startswith("output_full_") or suffix not in stem:
        return None
    body = stem[len("output_full_") :]
    task_id, run_s = body.rsplit(suffix, 1)
    if not task_id or not run_s.isdigit():
        return None
    return task_id, int(run_s)


def _field_str(v: Any) -> str:
    """CSV cell for a numeric/scalar field; preserve 0 (not blank)."""
    if v is None:
        return ""
    return str(v)


def _find_traj_on_disk(
    *,
    evaluation_root: Path,
    task_id: str,
    model_tag: str,
    run_idx: int,
    traj_path_raw: str,
    harness: str | None = None,
) -> Path:
    """Resolve traj path: post-hoc path first, then report run dir, then latest with traj."""
    candidates: list[Path] = []

    if traj_path_raw:
        p = _resolve_traj(traj_path_raw, evaluation_root)
        if p.is_file():
            return p
        candidates.append(p)

    if harness:
        run_name = f"output_full_{task_id}_{model_tag}_{harness}_{run_idx}"
        outputs_model = evaluation_root / "outputs" / harness / model_tag
        glob_pat = f"output_full_{task_id}_{model_tag}_{harness}_*/traj_{task_id}.json"
    else:
        run_name = f"output_full_{task_id}_{model_tag}_{run_idx}"
        outputs_model = evaluation_root / "outputs" / model_tag
        glob_pat = f"output_full_{task_id}_{model_tag}_*/traj_{task_id}.json"
    preferred = outputs_model / run_name / f"traj_{task_id}.json"
    candidates.append(preferred)

    globbed = sorted(
        outputs_model.glob(glob_pat),
        key=lambda p: int(p.parent.name.rsplit("_", 1)[-1]),
    )
    if globbed:
        return globbed[-1]

    for p in candidates:
        if p.is_file():
            return p
    return Path()


def _validate_rows(
    rows: list[dict[str, str]],
    *,
    repo_root: Path,
    tasks_root: Path,
    expected_tasks: int,
    expected_models: int,
) -> list[str]:
    warnings: list[str] = []
    valid_tasks = {p.parent.name for p in tasks_root.glob("*/task_config.json")}

    pair_counts: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["task"], r["model_tag"])
        pair_counts[key] = pair_counts.get(key, 0) + 1

    dup_pairs = [k for k, n in pair_counts.items() if n > 1]
    if dup_pairs:
        warnings.append(f"duplicate (task, model_tag) rows: {len(dup_pairs)}")

    unknown_tasks = sorted({r["task"] for r in rows if r["task"] not in valid_tasks})
    if unknown_tasks:
        warnings.append(f"task ids not in tasks/: {unknown_tasks}")

    missing_traj = [
        (r["task"], r["model_tag"])
        for r in rows
        if r["output_file"] and not (repo_root / r["output_file"]).is_file()
    ]
    if missing_traj:
        warnings.append(f"missing traj files: {len(missing_traj)} e.g. {missing_traj[0]}")

    expected_rows = expected_tasks * expected_models
    if len(rows) != expected_rows:
        warnings.append(f"row count {len(rows)} != {expected_tasks}x{expected_models}={expected_rows}")

    missing_pairs = []
    for t in sorted(valid_tasks):
        for tag in sorted({r["model_tag"] for r in rows}):
            if pair_counts.get((t, tag), 0) == 0:
                missing_pairs.append((t, tag))
    if missing_pairs:
        warnings.append(f"missing (task, model) pairs: {len(missing_pairs)} e.g. {missing_pairs[0]}")

    return warnings


def _bool_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _collect_reports(report_root: Path) -> list[tuple[Path, str | None]]:
    """``(report, harness)``. OpenHands reports have harness None.

    Another harness stores reports one directory deeper:
    ``<root>/<harness>/<setting>/output_full_*.json``.
    """
    reports: list[tuple[Path, str | None]] = []
    if not report_root.is_dir():
        return reports
    for model_dir in sorted(report_root.iterdir()):
        if not model_dir.is_dir():
            continue
        if model_dir.name.startswith("scenario-probe"):
            continue
        for p in sorted(model_dir.glob("output_full_*.json")):
            reports.append((p, None))
        for setting_dir in sorted(p for p in model_dir.iterdir() if p.is_dir()):
            if setting_dir.name.startswith("scenario-probe"):
                continue
            for p in sorted(setting_dir.glob("output_full_*.json")):
                harness = None if model_dir.name.startswith("openhands-") else model_dir.name
                reports.append((p, harness))
    return reports


def build_rows(
    *,
    repo_root: Path,
    evaluation_root: Path,
    report_root: Path,
    tasks_root: Path,
) -> list[dict[str, str]]:
    profile_tags = load_llm_profile_tags(evaluation_root / "config.toml")
    tag_to_profile = _invert_profile_tags(profile_tags)
    meta_cache: dict[str, dict[str, str]] = {}
    rows: list[dict[str, str]] = []

    for report_path, harness in _collect_reports(report_root):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        model_tag = report_path.parent.name
        parsed = _parse_report_filename(report_path, model_tag, harness=harness)
        json_task_id = str(report.get("task_id", "") or "")
        if parsed:
            task_id, run_idx = parsed
        else:
            task_id, run_idx = json_task_id, 1

        agent_profile = tag_to_profile.get(model_tag, "")

        traj_abs = _find_traj_on_disk(
            evaluation_root=evaluation_root,
            task_id=task_id,
            model_tag=model_tag,
            run_idx=run_idx,
            traj_path_raw=str(report.get("traj_path", "") or ""),
            harness=harness,
        )
        meta = _task_meta(task_id, tasks_root=tasks_root, repo_root=repo_root, cache=meta_cache)

        rows.append(
            {
                "task": task_id,
                "agent_profile": agent_profile,
                "model_tag": model_tag,
                "model_display": MODEL_DISPLAY.get(agent_profile, model_tag),
                "output_file": _to_rel(traj_abs, repo_root) if traj_abs.name else "",
                "report_file": _to_rel(report_path, repo_root),
                "domain": meta["domain"],
                "harm": meta["harm"],
                "situational_trigger": meta["situational_trigger"],
                "completion": _field_str(report.get("completion_answer")),
                "violation": _field_str(report.get("violation_answer")),
                "safe_completion": _bool_str(report.get("safe_completion")),
                "duration_seconds": _field_str(report.get("duration_seconds")),
                "agent_iterations": _field_str(report.get("agent_iterations")),
                "agent_iterations_max": _field_str(report.get("agent_iterations_max")),
                "agent_user_messages": _field_str(report.get("agent_user_messages")),
                "termination_reason": _field_str(report.get("termination_reason")),
                "prompt_tokens": _field_str(report.get("prompt_tokens")),
                "completion_tokens": _field_str(report.get("completion_tokens")),
                "reasoning_tokens": _field_str(report.get("reasoning_tokens")),
                "reasoning_share": _field_str(report.get("reasoning_share")),
                "cache_read_tokens": _field_str(report.get("cache_read_tokens")),
                "cache_write_tokens": _field_str(report.get("cache_write_tokens")),
                "cost_usd_table": _field_str(report.get("cost_usd_table")),
                "cost_usd_provider": _field_str(report.get("cost_usd_provider")),
                "cost_priced_by_table": _bool_str(report.get("cost_priced_by_table")),
                "total_steps": _field_str(report.get("total_steps")),
                "total_webapp_actions": _field_str(report.get("total_webapp_actions")),
            }
        )

    rows.sort(key=lambda r: (r["task"], r["agent_profile"]))
    return rows


def build_parser() -> argparse.ArgumentParser:
    repo_root = infer_repo_root()
    evaluation_root = repo_root / "authorization_eva_sandbox" / "evaluation"
    default_out = evaluation_root / "outputs" / "eval_full_504.csv"
    p = argparse.ArgumentParser(description="Build full eval analysis CSV from post-hoc reports")
    p.add_argument("--repo-root", type=Path, default=repo_root)
    p.add_argument("--evaluation-root", type=Path, default=evaluation_root)
    p.add_argument(
        "--report-root",
        type=Path,
        default=evaluation_root / "detectors" / "post_hoc_eval_report",
    )
    p.add_argument("--tasks-root", type=Path, default=repo_root / "authorization_eva_sandbox" / "tasks")
    p.add_argument("--output", type=Path, default=default_out)
    p.add_argument("--expected-rows", type=int, default=504)
    return p


def main() -> int:
    args = build_parser().parse_args()
    rows = build_rows(
        repo_root=args.repo_root,
        evaluation_root=args.evaluation_root,
        report_root=args.report_root,
        tasks_root=args.tasks_root,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[eval_full] rows={len(rows)} output={args.output}")

    warnings = _validate_rows(
        rows,
        repo_root=args.repo_root,
        tasks_root=args.tasks_root,
        expected_tasks=84,
        expected_models=6,
    )
    for w in warnings:
        print(f"[eval_full] WARN: {w}", file=sys.stderr)

    if args.expected_rows and len(rows) != args.expected_rows:
        print(
            f"[eval_full] WARN: expected {args.expected_rows} rows, got {len(rows)}",
            file=sys.stderr,
        )
        return 1
    if warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
