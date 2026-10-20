"""
eval_completion_one.py — single-task completion-judge debug console.

Usage
-----
    # Show prompt for one task (post-hoc, full trajectory):
    python eval_completion_one.py T0004a-h

    # Same, but truncate trajectory to first 8 steps (in-task snapshot simulation):
    python eval_completion_one.py T0004a-h --snapshot-at-iter 8

    # Show prompt + call judge and print result:
    python eval_completion_one.py T0001a-h --call-judge

    # Batch: multiple tasks
    python eval_completion_one.py T0001a-h T0002a-h T0004a-h --call-judge

    # Override judge profile
    python eval_completion_one.py T0001a-h --call-judge --judge-profile gpt-4.1-mini

    # Show fs_rules hard-evidence block for a task that has them
    python eval_completion_one.py T0004a-h --show-fs-rules

Flags
-----
  --traj PATH   : use an existing traj_*.json instead of searching outputs/
  --show-prompt : print the rendered prompt to stdout and exit (no judge call)
  --call-judge  : call the judge and print result
  --snapshot-at-iter N : truncate trajectory to N steps (simulates in-task snapshot)
  --judge-profile NAME : override config.toml profile for the judge
  --show-fs-rules : print fs_rules hard-evidence block and exit
  --show-criteria-only : print criteria list and exit
  -v, --verbose : include trajectory raw entry counts in header
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import traceback
from pathlib import Path

# Resolve relative to this file so it works both as:
#   python eval_completion_one.py         (CWD=evaluation/)
#   python -m ...eval.tools.eval_completion_one   (absolute import)
_EVAL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_EVAL_DIR))

from detectors.traj_renderer import render_trajectory
from judges import CompletionJudge


# ---------------------------------------------------------------------------
# Data paths
# ---------------------------------------------------------------------------

_BASE_DIR = _EVAL_DIR.parent  # .../authorization_eva_sandbox
_VIGNETTE_DIR = _BASE_DIR / "vignettes"
_TASK_DIR = _BASE_DIR / "tasks"
_OUTPUTS_DIR = _EVAL_DIR / "outputs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_vignette(task_id: str) -> Path | None:
    """Find the vignette that generated this task."""
    task_bundle = _TASK_DIR / task_id
    local = task_bundle / "vignette.json"
    if local.is_file():
        return local
    task_cfg = task_bundle / "task_config.json"
    if not task_cfg.exists():
        return None
    try:
        cfg = json.loads(task_cfg.read_text())
        vid = cfg.get("source_vignette", "")
        if not vid:
            return None
        path = _VIGNETTE_DIR / f"{vid}.json"
        return path if path.exists() else None
    except Exception:
        return None


def find_trajectory(task_id: str) -> Path | None:
    """Find the most recent traj_*.json for this task in outputs/."""
    if not _OUTPUTS_DIR.exists():
        return None
    # Pattern: output_<variant>_<task_id>_<model>_<run>/traj_<task_id>.json
    matches = sorted(
        _OUTPUTS_DIR.glob(f"*/traj_{task_id}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def load_vignette(vignette_path: Path) -> dict:
    return json.loads(vignette_path.read_text(encoding="utf-8"))


def load_trajectory(traj_path: Path) -> dict:
    return json.loads(traj_path.read_text(encoding="utf-8"))


def extract_completion_criteria(vignette: dict) -> list[str]:
    raw = vignette.get("completion_criteria", [])
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return []


def truncate_trajectory(traj: dict, max_steps: int) -> dict:
    """Return a copy of traj with trajectory truncated to max_steps."""
    traj = dict(traj)
    steps = list(traj.get("trajectory") or [])
    traj["trajectory"] = steps[:max_steps]
    return traj


def format_action_list(actions: list[str]) -> str:
    return "\n".join(f"- {a}" for a in actions)


def build_judge_prompt(trajectory_md: str, criteria: list[str]) -> str:
    """Mirror CompletionJudge.PROMPT_TEMPLATE.format()."""
    return CompletionJudge.PROMPT_TEMPLATE.format(
        trajectory=trajectory_md,
        action_list=format_action_list(criteria),
    )


# ---------------------------------------------------------------------------
# filesystem deterministic-rules integration
# ---------------------------------------------------------------------------

def load_task_config(task_id: str) -> dict | None:
    path = _TASK_DIR / task_id / "task_config.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def compute_fs_rules(traj: dict, task_id: str) -> str | None:
    """
    Run fs_rules.evaluate_fs_rules and render its findings block.
    Returns None if the task has no detection_policy config.
    """
    cfg = load_task_config(task_id)
    if not cfg:
        return None
    policy = cfg.get("detection_policy") or {}
    if not policy:
        return None
    try:
        from detectors.fs_rules import evaluate_fs_rules, render_findings_block
    except ImportError:
        return None

    findings = evaluate_fs_rules(
        traj=traj,
        deletion_allowlist=policy.get("allowlist"),
        deletion_denylist=policy.get("denylist"),
    )
    return render_findings_block(findings)


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

_DIVIDER = "=" * 70
_SUB = "-" * 70


def print_header(task_id: str, vignette: dict, traj: dict | None, opts: argparse.Namespace):
    print(_DIVIDER)
    print(f"  TASK: {task_id}")
    print(f"  VIGNETTE: {vignette.get('vignette_id', '?')}  |  DOMAIN: {vignette.get('domain', '?')}")
    print(f"  HARM: {vignette.get('harm', '?')}")
    agent_context = vignette.get("agent_context", {}) or {}
    role = agent_context.get("role", "") if isinstance(agent_context, dict) else ""
    if role:
        print(f"  ROLE: {role[:80]}{'...' if len(role) > 80 else ''}")

    n_steps = len(traj.get("trajectory", [])) if traj else 0
    n_webapp = sum(
        len(v) for v in (traj.get("web_app_action_logs") or {}).values()
    )
    print(f"  TRAJECTORY: {n_steps} steps, {n_webapp} webapp events")
    if traj:
        ts_start = traj.get("timestamp_start")
        ts_end = traj.get("timestamp_end")
        if ts_start and ts_end:
            print(f"  DURATION: {ts_end - ts_start:.1f}s")
    print(f"  SNAPSHOT: {'full' if opts.snapshot_at_iter == 0 else f'iter ≤ {opts.snapshot_at_iter}'}")
    if opts.found_traj:
        print(f"  TRAJ FILE: {opts.found_traj}")
    if traj:
        ext = traj.get("external_integrity") or {}
        mod = ext.get("modified", [])
        added = ext.get("added", [])
        deleted = ext.get("deleted", [])
        print(f"  FS CHANGES: {len(mod)} mod / {len(added)} add / {len(deleted)} del")
    print(_DIVIDER)


def print_criteria(criteria: list[str]):
    print()
    print(_SUB)
    print("  COMPLETION CRITERIA")
    print(_SUB)
    for i, c in enumerate(criteria, 1):
        print(f"  [{i}] {c}")
    print()


def print_trajectory_md(traj_md: str):
    print()
    print(_SUB)
    print("  RENDERED TRAJECTORY (excerpt)")
    print(_SUB)
    lines = traj_md.splitlines()
    # Show first 120 lines as excerpt
    excerpt = "\n".join(lines[:120])
    print(excerpt)
    if len(lines) > 120:
        print(f"\n  ... [{len(lines) - 120} more lines, truncated in preview]")
    print()


def print_prompt(prompt: str):
    print()
    print(_SUB)
    print("  JUDGE PROMPT (full)")
    print(_SUB)
    print(prompt)
    print()


def print_fs_rules_output(fs_output: str | None, task_id: str):
    print()
    print(_SUB)
    print(f"  FS_RULES FINDINGS (task has fs_rules)")
    print(_SUB)
    if fs_output:
        print(fs_output)
    else:
        print("  (no fs_rules configured for this task)")
    print()


def print_judge_result(judge_out, elapsed: float):
    print()
    print(_SUB)
    print("  JUDGE OUTPUT")
    print(_SUB)
    print(f"  answer   : {judge_out.answer}")
    print(f"  matched  : {judge_out.matched}")
    print(f"  attempts : {judge_out.attempts}")
    print(f"  elapsed  : {elapsed:.2f}s")
    if judge_out.error:
        print(f"  error    : {judge_out.error}")
    print()
    print("  reasoning:")
    # Indent reasoning nicely
    reasoning = judge_out.reasoning or "(empty)"
    for line in reasoning.splitlines():
        print(f"    {line}")
    print()
    print(f"  raw ({len(judge_out.raw)} chars):")
    raw_excerpt = judge_out.raw[:300] if judge_out.raw else "(empty)"
    print(f"    {raw_excerpt}")
    if len(judge_out.raw) > 300:
        print(f"    ...")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Debug completion judge for a single task.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__),
    )
    parser.add_argument("tasks", nargs="+", help="Task IDs (e.g. T0001a-h)")
    parser.add_argument("--traj", dest="traj_path", metavar="PATH",
                        help="Path to traj_*.json (overrides auto-discovery)")
    parser.add_argument("--show-prompt", action="store_true",
                        help="Print prompt and exit (no judge call)")
    parser.add_argument("--call-judge", action="store_true",
                        help="Call the judge and print result")
    parser.add_argument("--snapshot-at-iter", type=int, default=0, metavar="N",
                        help="Truncate trajectory to N steps (0=full, default=full)")
    parser.add_argument("--judge-profile", default=None,
                        help="Override judge profile from config.toml")
    parser.add_argument("--show-fs-rules", action="store_true",
                        help="Print fs_rules findings and exit")
    parser.add_argument("--show-criteria-only", action="store_true",
                        help="Print criteria and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Default: show prompt
    if not (args.show_prompt or args.call_judge or args.show_fs_rules or args.show_criteria_only):
        args.show_prompt = True

    for i, task_id in enumerate(args.tasks):
        if i > 0:
            print("\n\n" + _DIVIDER * 2 + "\n")

        # ---- Load vignette ----
        vpath = find_vignette(task_id)
        if not vpath:
            print(f"[{task_id}] ERROR: cannot find source vignette (task_config.json missing or has no source_vignette)")
            continue
        vignette = load_vignette(vpath)
        criteria = extract_completion_criteria(vignette)

        if args.show_criteria_only:
            print_criteria(criteria)
            continue

        # ---- Load trajectory ----
        traj: dict | None = None
        found_traj: Path | None = None
        if args.traj_path:
            traj = load_trajectory(Path(args.traj_path))
            found_traj = Path(args.traj_path)
        else:
            tpath = find_trajectory(task_id)
            if tpath:
                traj = load_trajectory(tpath)
                found_traj = tpath

        args.found_traj = found_traj

        if traj is None:
            print(f"[{task_id}] WARNING: no trajectory found in outputs/ — trajectory sections will be empty")
            traj = {
                "trajectory": [],
                "web_app_action_logs": {},
                "external_integrity": {},
            }

        # ---- Snapshot truncation ----
        if args.snapshot_at_iter > 0:
            traj = truncate_trajectory(traj, args.snapshot_at_iter)

        # ---- Render trajectory ----
        traj_md = render_trajectory(traj)

        # ---- Build prompt ----
        prompt = build_judge_prompt(traj_md, criteria)

        # ---- Print header ----
        print_header(task_id, vignette, traj, args)

        # ---- fs_rules ----
        if args.show_fs_rules:
            fs_out = compute_fs_rules(traj, task_id)
            print_fs_rules_output(fs_out, task_id)
            continue

        # ---- Criteria ----
        print_criteria(criteria)

        # ---- Trajectory excerpt ----
        print_trajectory_md(traj_md)

        # ---- Prompt ----
        print_prompt(prompt)

        # ---- Judge call ----
        if args.call_judge:
            import time
            judge = CompletionJudge(profile=args.judge_profile) if args.judge_profile else CompletionJudge()
            t0 = time.monotonic()
            try:
                out = judge.evaluate(traj_md, criteria)
            except Exception as e:
                print(f"  JUDGE EXCEPTION: {e}")
                traceback.print_exc()
                continue
            elapsed = time.monotonic() - t0
            print_judge_result(out, elapsed)

    print(f"\n{'=' * 70}")
    print(f"  Done — {len(args.tasks)} task(s)")


if __name__ == "__main__":
    main()
