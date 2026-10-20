#!/usr/bin/env python3
"""Batch runner for sandbox tasks with resilient post-hoc evaluation.

Modes:
- **task-first** (default): for each task, run missing agent models sequentially,
  skip (task, model) pairs that already have trajectories on disk, optionally
  backfill post-hoc only when a trajectory exists but no report does.
- **model-first** (legacy): run all tasks per agent model.

Safety defaults for WSL/Docker:
- one run_eval at a time; docker rm before each run
- infra-error retry with backoff
- async post-hoc worker (may overlap with the next run_eval within a task)
- wait for each task's post-hoc jobs before starting the next task
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis_feature_utils import TURN_LEVEL_FIELDS, compute_turn_metrics_from_traj


DEFAULT_AGENT_SEQUENCE = (
    "gpt5_5,gpt4_1,sonnet,gemini_3_1_pro,"
    "qwen3_vl_30b_a3b_instruct,qwen3_vl_30b_a3b_thinking"
)

RUN_TERMINAL = {"ok", "failed", "timeout", "infra_failed", "error"}
POSTHOC_TERMINAL = {"ok", "inconclusive", "failed", "error", "skipped"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def record_key(task_id: str, agent_profile: str = "") -> str:
    if agent_profile:
        return f"{task_id}::{agent_profile}"
    return task_id


def list_task_ids(tasks_root: Path) -> list[str]:
    task_ids = []
    for cfg in sorted(tasks_root.glob("*/task_config.json")):
        task_ids.append(cfg.parent.name)
    return task_ids


def _model_short_name(model: str) -> str:
    short = model.rsplit("/", 1)[-1]
    return short.replace(" ", "_")


def load_llm_profile_tags(config_path: Path) -> dict[str, str]:
    """Map config.toml [llm.profile] names to output directory model tags."""
    profiles: dict[str, str] = {}
    current: str | None = None
    if not config_path.is_file():
        return profiles
    for line in config_path.read_text(encoding="utf-8").splitlines():
        section = re.match(r"^\[llm\.(.+)\]\s*$", line.strip())
        if section:
            current = section.group(1)
            continue
        if current and line.strip().startswith("model"):
            _, _, raw = line.partition("=")
            model = raw.strip().strip('"').strip("'")
            profiles[current] = _model_short_name(model)
    return profiles


def is_infra_error(text: str) -> bool:
    blob = text.lower()
    markers = [
        "connection error",
        "connection reset",
        "connection aborted",
        "readerror",
        "errno 104",
        "dockerexception",
        "api connection",
        "max retries exceeded",
        "temporarily unavailable",
        "internalservererror",
        "openaiexception",
        "rate limit",
        "wait_until_alive",
    ]
    return any(m in blob for m in markers)


def _venv_env(python_bin: str) -> dict[str, str]:
    """Ensure subprocesses use the project venv even when the parent lacks VIRTUAL_ENV."""
    env = os.environ.copy()
    py = Path(python_bin).resolve()
    venv_root = py.parent.parent
    env["VIRTUAL_ENV"] = str(venv_root)
    env["PATH"] = f"{py.parent}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONHOME", None)
    return env


def run_cmd(cmd: list[str], cwd: Path, timeout_s: float, python_bin: str | None = None) -> dict[str, Any]:
    start = time.time()
    env = _venv_env(python_bin) if python_bin else None
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_s if timeout_s > 0 else None,
            check=False,
            env=env,
        )
        return {
            "timed_out": False,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_s": round(time.time() - start, 3),
        }
    except subprocess.TimeoutExpired as e:
        def _norm(v: Any) -> str:
            if v is None:
                return ""
            if isinstance(v, bytes):
                return v.decode("utf-8", errors="replace")
            return str(v)

        return {
            "timed_out": True,
            "returncode": None,
            "stdout": _norm(e.stdout),
            "stderr": _norm(e.stderr),
            "duration_s": round(time.time() - start, 3),
        }


def docker_rm_runtime(task_id: str) -> None:
    name = f"openhands-runtime-{task_id}"
    run_cmd(["docker", "rm", "-f", name], cwd=Path("/"), timeout_s=60)


def assert_no_concurrent_run_eval() -> None:
    proc = subprocess.run(
        ["pgrep", "-af", "run_eval.py"],
        text=True,
        capture_output=True,
        check=False,
    )
    lines = [
        ln
        for ln in (proc.stdout or "").splitlines()
        if "run_eval.py" in ln and "pgrep" not in ln and "batch_run_eval" not in ln
    ]
    if lines:
        raise RuntimeError(
            "Another run_eval.py is already running; stop it before starting a batch:\n"
            + "\n".join(lines[:5])
        )


def find_latest_traj(outputs_root: Path, task_id: str, min_mtime: float) -> Path | None:
    candidates = []
    pattern = f"**/traj_{task_id}.json"
    for p in outputs_root.glob(pattern):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= (min_mtime - 5):
            candidates.append((mtime, p))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]


def find_existing_traj_for_model(
    outputs_root: Path,
    task_id: str,
    model_tag: str,
    harness: str = "openhands",
) -> Path | None:
    if harness and harness != "openhands":
        model_dir = outputs_root / harness / model_tag
        pattern = f"output_full_{task_id}_{model_tag}_{harness}_*"
    else:
        model_dir = outputs_root / model_tag
        pattern = f"output_full_{task_id}_{model_tag}_*"
    if not model_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for out_dir in model_dir.glob(pattern):
        traj = out_dir / f"traj_{task_id}.json"
        if traj.is_file():
            candidates.append((traj.stat().st_mtime, traj))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]


def find_existing_posthoc_report(
    evaluation_root: Path,
    task_id: str,
    model_tag: str,
    harness: str = "openhands",
) -> Path | None:
    report_root = evaluation_root / "detectors" / "post_hoc_eval_report"
    if harness and harness != "openhands":
        dirs = [report_root / harness / model_tag]
        pattern = f"output_full_{task_id}_{model_tag}_{harness}_*.md"
    else:
        dirs = [
            report_root / "openhands-native" / model_tag,
            report_root / "openhands-sealed" / model_tag,
            report_root / model_tag,
        ]
        pattern = f"output_full_{task_id}_{model_tag}_*.md"
    candidates = [
        path
        for report_dir in dirs
        if report_dir.is_dir()
        for path in report_dir.glob(pattern)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def parse_posthoc_labels(output: str) -> tuple[str | None, str | None]:
    completion = None
    violation = None
    m1 = re.search(r"completion\s*:\s*([a-zA-Z_]+)", output)
    m2 = re.search(r"violation\s*:\s*([a-zA-Z_]+)", output)
    if m1:
        completion = m1.group(1).lower()
    if m2:
        violation = m2.group(1).lower()
    return completion, violation


def _extract_model_from_traj(task_id: str, traj_path: str) -> str:
    if not traj_path:
        return ""
    run_dir = Path(traj_path).parent
    m = re.match(rf"^output_full_{re.escape(task_id)}_(.+)_\d+$", run_dir.name)
    if not m:
        return ""
    model = m.group(1)
    from authorization_eva_sandbox.evaluation.harness.layout import harness_from_run_dir

    harness = harness_from_run_dir(run_dir)
    if harness and model.endswith("_" + harness):
        model = model[: -(len(harness) + 1)]
    return model


def _to_rel(path_str: str, repo_root: Path) -> str:
    if not path_str:
        return ""
    p = Path(path_str)
    try:
        return str(p.relative_to(repo_root))
    except ValueError:
        return str(p)


def _task_meta(task_id: str, tasks_root: Path, repo_root: Path, cache: dict[str, dict[str, str]]) -> dict[str, str]:
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


def _safe_slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")


def _suffix_path(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.stem}_{suffix}{path.suffix}")


@dataclass
class BatchState:
    status_file: Path
    summary_csv: Path
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, task_id: str, agent_profile: str = "", **fields: Any) -> None:
        key = record_key(task_id, agent_profile)
        with self.lock:
            row = dict(self.records.get(key, {}))
            row["task_id"] = task_id
            if agent_profile:
                row["agent_profile"] = agent_profile
            row.update(fields)
            row["updated_at"] = utc_now_iso()
            self.records[key] = row
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            with self.status_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def load_latest(self) -> None:
        if not self.status_file.exists():
            return
        with self.status_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = row.get("task_id")
                if not isinstance(tid, str):
                    continue
                profile = str(row.get("agent_profile", "") or "")
                self.records[record_key(tid, profile)] = row

    def write_summary_csv(self, tasks_root: Path, repo_root: Path) -> None:
        with self.lock:
            self.summary_csv.parent.mkdir(parents=True, exist_ok=True)
            rows = sorted(
                self.records.values(),
                key=lambda x: (x.get("task_id", ""), x.get("agent_profile", "")),
            )
            if not rows:
                return
            cols = [
                "task",
                "agent_profile",
                "model",
                "output_file",
                "time_spent",
                "domain",
                "harm",
                "situational_trigger",
                "completion",
                "violation",
                "run_status",
                "posthoc_status",
            ]
            cols.extend(TURN_LEVEL_FIELDS)
            meta_cache: dict[str, dict[str, str]] = {}
            out_rows: list[dict[str, Any]] = []
            for r in rows:
                task_id = str(r.get("task_id", "") or "")
                traj_path = str(r.get("traj_path", "") or "")
                meta = _task_meta(task_id, tasks_root=tasks_root, repo_root=repo_root, cache=meta_cache)
                turn_metrics = compute_turn_metrics_from_traj(traj_path)
                out_rows.append(
                    {
                        "task": task_id,
                        "agent_profile": r.get("agent_profile", ""),
                        "model": _extract_model_from_traj(task_id, traj_path),
                        "output_file": _to_rel(traj_path, repo_root=repo_root),
                        "time_spent": r.get("run_duration_s", ""),
                        "domain": meta["domain"],
                        "harm": meta["harm"],
                        "situational_trigger": meta["situational_trigger"],
                        "completion": r.get("posthoc_completion", ""),
                        "violation": r.get("posthoc_violation", ""),
                        "run_status": r.get("run_status", ""),
                        "posthoc_status": r.get("posthoc_status", ""),
                        **turn_metrics,
                    }
                )
            with self.summary_csv.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=cols)
                writer.writeheader()
                writer.writerows(out_rows)


def should_skip_legacy(rec: dict[str, Any], do_posthoc: bool, rerun_failed: bool) -> bool:
    run_status = rec.get("run_status")
    posthoc_status = rec.get("posthoc_status")
    run_done = run_status in RUN_TERMINAL
    post_done = (not do_posthoc) or (posthoc_status in POSTHOC_TERMINAL)

    if not run_done:
        return False
    if not rerun_failed:
        return post_done
    return run_status == "ok" and post_done


def should_skip_task_model_pair(
    *,
    traj: Path | None,
    report: Path | None,
    rec: dict[str, Any],
    do_posthoc: bool,
    rerun_failed: bool,
) -> tuple[bool, str]:
    """Return (skip?, reason)."""
    if rerun_failed:
        run_status = rec.get("run_status")
        posthoc_status = rec.get("posthoc_status")
        if run_status not in RUN_TERMINAL or (
            do_posthoc and posthoc_status not in POSTHOC_TERMINAL
        ):
            return False, ""

    if traj is None:
        return False, ""

    if not do_posthoc:
        return True, "traj_exists"

    if report is not None:
        return True, "traj_and_report_exist"

    posthoc_status = rec.get("posthoc_status")
    if posthoc_status in {"ok", "inconclusive"}:
        return True, "posthoc_done_in_ledger"

    return False, ""


def _resolve_python_bin(python_bin: str, repo_root: Path) -> str:
    p = Path(python_bin)
    if p.is_absolute():
        return str(p)
    return str((repo_root / p).resolve())


def build_run_eval_cmd(args: argparse.Namespace, task_id: str, agent_model: str) -> list[str]:
    python_bin = _resolve_python_bin(args.python_bin, args.repo_root)
    cmd = [
        python_bin,
        "run_eval.py",
        "--task-path",
        str(Path("..") / "tasks" / task_id),
        "--outputs-path",
        args.outputs_path,
        "--server-hostname",
        args.server_hostname,
        "--agent-llm-config",
        agent_model,
        "--env-llm-config",
        args.env_llm_config,
        "--task-timeout-seconds",
        str(args.task_timeout_seconds),
    ]
    return cmd


def run_eval_with_retries(
    args: argparse.Namespace,
    state: BatchState,
    task_id: str,
    agent_profile: str,
) -> tuple[dict[str, Any], int]:
    attempt = 0
    final: dict[str, Any] | None = None
    while True:
        if not args.skip_docker_cleanup:
            docker_rm_runtime(task_id)
            if args.sleep_before_run > 0:
                time.sleep(args.sleep_before_run)

        cmd = build_run_eval_cmd(args, task_id, agent_profile)
        res = run_cmd(
            cmd,
            cwd=args.evaluation_root,
            timeout_s=args.run_timeout_seconds,
            python_bin=_resolve_python_bin(args.python_bin, args.repo_root),
        )
        text = (res["stdout"] or "") + "\n" + (res["stderr"] or "")
        final = res
        if (not res["timed_out"]) and res["returncode"] == 0:
            break
        if attempt >= args.retry_infra_failures or not is_infra_error(text):
            if res["returncode"] != 0:
                tail = text.strip()[-1500:]
                print(f"[batch] run_eval failed for {task_id}/{agent_profile}:\n{tail}")
            break
        attempt += 1
        backoff = min(60.0, max(8.0, 2.0 ** attempt * 4))
        state.update(
            task_id,
            agent_profile,
            run_retry_attempt=attempt,
            run_retry_reason="infra_error",
        )
        print(f"[batch] infra retry {attempt}/{args.retry_infra_failures} for {task_id}/{agent_profile} in {backoff:.0f}s")
        time.sleep(backoff)

    assert final is not None
    return final, attempt


def posthoc_worker(
    q: queue.Queue,
    state: BatchState,
    python_bin: str,
    repo_root: Path,
    judge_profile: str,
    sleep_before_posthoc: float,
    posthoc_timeout_s: float,
) -> None:
    while True:
        item = q.get()
        if item is None:
            q.task_done()
            return

        task_id = item["task_id"]
        agent_profile = item.get("agent_profile", "")
        traj_path = Path(item["traj_path"])
        done_event: threading.Event | None = item.get("done_event")
        if sleep_before_posthoc > 0:
            time.sleep(sleep_before_posthoc)

        state.update(
            task_id,
            agent_profile,
            posthoc_started_at=utc_now_iso(),
            posthoc_status="running",
            judge_profile=judge_profile,
        )
        cmd = [
            _resolve_python_bin(python_bin, repo_root),
            "-m",
            "authorization_eva_sandbox.evaluation.post_hoc_eval",
            "--trajectory",
            str(traj_path),
            "--judge-profile",
            judge_profile,
            "--verbose",
        ]
        res = run_cmd(
            cmd,
            cwd=repo_root,
            timeout_s=posthoc_timeout_s,
            python_bin=_resolve_python_bin(python_bin, repo_root),
        )
        combined = (res["stdout"] or "") + "\n" + (res["stderr"] or "")
        completion, violation = parse_posthoc_labels(combined)

        if res["timed_out"]:
            status = "error"
            err = "post_hoc_timeout"
        elif res["returncode"] != 0:
            status = "failed"
            err = f"post_hoc_exit_{res['returncode']}"
        elif completion is None or violation is None:
            status = "inconclusive"
            err = "post_hoc_parse_missing_labels"
        elif completion == "inconclusive" or violation == "inconclusive":
            status = "inconclusive"
            err = "judge_inconclusive"
        else:
            status = "ok"
            err = None

        state.update(
            task_id,
            agent_profile,
            posthoc_finished_at=utc_now_iso(),
            posthoc_status=status,
            posthoc_exit_code=res["returncode"],
            posthoc_duration_s=res["duration_s"],
            posthoc_completion=completion,
            posthoc_violation=violation,
            posthoc_error=err,
        )
        if done_event is not None:
            done_event.set()
        q.task_done()


def build_parser() -> argparse.ArgumentParser:
    repo_root = infer_repo_root()
    p = argparse.ArgumentParser(
        description="Batch run run_eval + post_hoc_eval (task-first or model-first)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--python-bin", default="", help="Python interpreter path (default: sys.executable)")
    p.add_argument("--repo-root", type=Path, default=repo_root, help="Repository root")
    p.add_argument("--tasks-root", type=Path, default=repo_root / "authorization_eva_sandbox" / "tasks")
    p.add_argument("--evaluation-root", type=Path, default=repo_root / "authorization_eva_sandbox" / "evaluation")
    p.add_argument("--outputs-path", default="./outputs", help="Passed to run_eval --outputs-path")
    p.add_argument("--server-hostname", default="localhost")
    p.add_argument("--order", choices=["task-first", "model-first"], default="task-first")
    p.add_argument("--agent-llm-config", default="gpt4_1")
    p.add_argument(
        "--agent-llm-sequence",
        default=DEFAULT_AGENT_SEQUENCE,
        help="Comma-separated agent profiles for task-first mode",
    )
    p.add_argument("--env-llm-config", default="opus_4_7", help="Oracle / env LLM profile")
    p.add_argument("--judge-profile", default="opus_4_7", help="post_hoc_eval --judge-profile")
    p.add_argument("--task-timeout-seconds", type=float, default=600.0, help="Passed to run_eval")
    p.add_argument("--run-timeout-seconds", type=float, default=1800.0, help="External timeout per run_eval subprocess")
    p.add_argument("--posthoc-timeout-seconds", type=float, default=900.0)
    p.add_argument("--retry-infra-failures", type=int, default=2)
    p.add_argument("--sleep-before-run", type=float, default=15.0, help="Sleep after docker rm, before run_eval")
    p.add_argument("--sleep-between-runs", type=float, default=15.0, help="Sleep after each model within a task")
    p.add_argument("--sleep-between-tasks", type=float, default=30.0, help="Sleep after each task completes")
    p.add_argument("--sleep-before-posthoc", type=float, default=10.0)
    p.add_argument("--sleep-between-models", type=float, default=60.0, help="Model-first mode only")
    p.add_argument("--limit", type=int, default=0, help="0 means all")
    p.add_argument("--start-from", default="", help="Task id to start from (inclusive)")
    p.add_argument("--task-ids", nargs="*", default=[], help="Explicit task ids to run")
    p.add_argument("--no-posthoc", action="store_true")
    p.add_argument("--rerun-failed", action="store_true")
    p.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Ignore on-disk trajectories/reports and run_eval every (task, model) pair (pilot use)",
    )
    p.add_argument("--skip-docker-cleanup", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Print planned work and exit")
    p.add_argument("--skip-preflight", action="store_true", help="Do not check for concurrent run_eval.py")
    p.add_argument(
        "--status-file",
        type=Path,
        default=repo_root / "authorization_eva_sandbox" / "evaluation" / "outputs" / "batch_task_first.jsonl",
    )
    p.add_argument(
        "--summary-csv",
        type=Path,
        default=repo_root / "authorization_eva_sandbox" / "evaluation" / "outputs" / "batch_task_first.csv",
    )
    return p


def _resolve_outputs_root(args: argparse.Namespace) -> Path:
    outputs_arg = Path(args.outputs_path)
    return outputs_arg if outputs_arg.is_absolute() else (args.evaluation_root / outputs_arg)


def _select_task_ids(args: argparse.Namespace) -> list[str]:
    task_ids = args.task_ids or list_task_ids(args.tasks_root)
    if args.start_from:
        if args.start_from not in task_ids:
            raise ValueError(f"start-from task not found: {args.start_from}")
        start_idx = task_ids.index(args.start_from)
        task_ids = task_ids[start_idx:]
    if args.limit > 0:
        task_ids = task_ids[: args.limit]
    return task_ids


def _parse_models(args: argparse.Namespace) -> list[str]:
    if args.order == "task-first":
        raw = args.agent_llm_sequence.strip() or args.agent_llm_config
    else:
        raw = args.agent_llm_sequence.strip() or args.agent_llm_config
    models = [m.strip() for m in raw.split(",") if m.strip()]
    if not models:
        raise ValueError("No agent models configured")
    return models


def _plan_task_first_work(
    args: argparse.Namespace,
    task_ids: list[str],
    models: list[str],
    profile_tags: dict[str, str],
    outputs_root: Path,
    state: BatchState,
) -> list[dict[str, Any]]:
    do_posthoc = not args.no_posthoc
    plan: list[dict[str, Any]] = []
    for task_id in task_ids:
        for agent_profile in models:
            model_tag = profile_tags.get(agent_profile, agent_profile)
            traj = find_existing_traj_for_model(outputs_root, task_id, model_tag)
            report = (
                find_existing_posthoc_report(args.evaluation_root, task_id, model_tag)
                if do_posthoc
                else None
            )
            if not args.no_skip_existing:
                rec = state.records.get(record_key(task_id, agent_profile), {})
                skip, reason = should_skip_task_model_pair(
                    traj=traj,
                    report=report,
                    rec=rec,
                    do_posthoc=do_posthoc,
                    rerun_failed=args.rerun_failed,
                )
                if skip:
                    continue

            if (
                not args.no_skip_existing
                and traj is not None
                and do_posthoc
                and report is None
            ):
                action = "posthoc_only"
            elif args.no_skip_existing or traj is None:
                action = "run_eval"
            elif traj is not None and do_posthoc and report is None:
                action = "posthoc_only"
            else:
                continue
            plan.append(
                {
                    "task_id": task_id,
                    "agent_profile": agent_profile,
                    "model_tag": model_tag,
                    "action": action,
                    "existing_traj": str(traj) if traj else "",
                }
            )
    return plan


def run_task_first(args: argparse.Namespace) -> int:
    do_posthoc = not args.no_posthoc
    models = _parse_models(args)
    profile_tags = load_llm_profile_tags(args.evaluation_root / "config.toml")
    outputs_root = _resolve_outputs_root(args)

    state = BatchState(status_file=args.status_file, summary_csv=args.summary_csv)
    state.load_latest()
    task_ids = _select_task_ids(args)
    plan = _plan_task_first_work(args, task_ids, models, profile_tags, outputs_root, state)

    print(
        f"[batch] order=task-first tasks={len(task_ids)} models={len(models)} "
        f"pending_steps={len(plan)} posthoc={do_posthoc} judge={args.judge_profile} "
        f"env={args.env_llm_config}"
    )
    for step in plan:
        print(
            f"  plan {step['action']:11s} {step['task_id']} "
            f"agent={step['agent_profile']} tag={step['model_tag']}"
        )

    if args.dry_run:
        return 0

    if not args.skip_preflight:
        assert_no_concurrent_run_eval()

    run_queue: queue.Queue = queue.Queue()
    worker = None
    if do_posthoc:
        worker = threading.Thread(
            target=posthoc_worker,
            kwargs={
                "q": run_queue,
                "state": state,
                "python_bin": args.python_bin,
                "repo_root": args.repo_root,
                "judge_profile": args.judge_profile,
                "sleep_before_posthoc": args.sleep_before_posthoc,
                "posthoc_timeout_s": args.posthoc_timeout_seconds,
            },
            daemon=True,
        )
        worker.start()

    step_idx = 0
    current_task = None
    task_posthoc_events: list[threading.Event] = []

    for step in plan:
        task_id = step["task_id"]
        agent_profile = step["agent_profile"]
        model_tag = step["model_tag"]

        if current_task is not None and task_id != current_task:
            for ev in task_posthoc_events:
                ev.wait(timeout=args.posthoc_timeout_seconds + args.sleep_before_posthoc + 30)
            task_posthoc_events.clear()
            if args.sleep_between_tasks > 0:
                print(f"[batch] task {current_task} done; sleeping {args.sleep_between_tasks}s")
                time.sleep(args.sleep_between_tasks)
        current_task = task_id
        step_idx += 1

        if step["action"] == "posthoc_only":
            traj = Path(step["existing_traj"])
            state.update(
                task_id,
                agent_profile,
                run_status="ok",
                run_error=None,
                traj_path=str(traj),
                run_note="skipped_existing_traj",
            )
            if do_posthoc:
                ev = threading.Event()
                task_posthoc_events.append(ev)
                run_queue.put(
                    {
                        "task_id": task_id,
                        "agent_profile": agent_profile,
                        "traj_path": str(traj),
                        "done_event": ev,
                    }
                )
            continue

        print(f"[batch] ({step_idx}/{len(plan)}) run_eval {task_id} agent={agent_profile}")
        start_ts = time.time()
        state.update(task_id, agent_profile, run_started_at=utc_now_iso(), run_status="running")

        final, attempt = run_eval_with_retries(args, state, task_id, agent_profile)
        traj = find_latest_traj(outputs_root, task_id, min_mtime=start_ts)
        if not traj:
            traj = find_existing_traj_for_model(outputs_root, task_id, model_tag)

        if final["timed_out"]:
            run_status = "timeout"
            run_error = "run_eval_timeout"
        elif final["returncode"] == 0:
            run_status = "ok"
            run_error = None
        else:
            text = (final["stdout"] or "") + "\n" + (final["stderr"] or "")
            run_status = "infra_failed" if is_infra_error(text) else "failed"
            run_error = f"run_eval_exit_{final['returncode']}"

        state.update(
            task_id,
            agent_profile,
            run_finished_at=utc_now_iso(),
            run_status=run_status,
            run_exit_code=final["returncode"],
            run_duration_s=final["duration_s"],
            run_attempts=attempt + 1,
            run_error=run_error,
            traj_path=str(traj) if traj else "",
        )

        if do_posthoc:
            if traj and run_status == "ok":
                ev = threading.Event()
                task_posthoc_events.append(ev)
                run_queue.put(
                    {
                        "task_id": task_id,
                        "agent_profile": agent_profile,
                        "traj_path": str(traj),
                        "done_event": ev,
                    }
                )
            else:
                state.update(
                    task_id,
                    agent_profile,
                    posthoc_status="skipped",
                    posthoc_error="traj_not_found_or_run_failed",
                )

        if args.sleep_between_runs > 0:
            time.sleep(args.sleep_between_runs)

    if current_task is not None:
        for ev in task_posthoc_events:
            ev.wait(timeout=args.posthoc_timeout_seconds + args.sleep_before_posthoc + 30)
        task_posthoc_events.clear()

    if do_posthoc:
        run_queue.put(None)
        run_queue.join()
        if worker:
            worker.join(timeout=5)

    state.write_summary_csv(tasks_root=args.tasks_root, repo_root=args.repo_root)
    print(f"[batch] task-first done. status={args.status_file}")
    print(f"[batch] summary={args.summary_csv}")
    return 0


def run_for_model(args: argparse.Namespace, agent_model: str, status_file: Path, summary_csv: Path) -> Path:
    do_posthoc = not args.no_posthoc

    state = BatchState(status_file=status_file, summary_csv=summary_csv)
    state.load_latest()

    task_ids = _select_task_ids(args)

    run_queue: queue.Queue = queue.Queue()
    worker = None
    if do_posthoc:
        worker = threading.Thread(
            target=posthoc_worker,
            kwargs={
                "q": run_queue,
                "state": state,
                "python_bin": args.python_bin,
                "repo_root": args.repo_root,
                "judge_profile": args.judge_profile,
                "sleep_before_posthoc": args.sleep_before_posthoc,
                "posthoc_timeout_s": args.posthoc_timeout_seconds,
            },
            daemon=True,
        )
        worker.start()

    to_run: list[str] = []
    for tid in task_ids:
        rec = state.records.get(record_key(tid, agent_model), {})
        if should_skip_legacy(rec, do_posthoc=do_posthoc, rerun_failed=args.rerun_failed):
            continue
        to_run.append(tid)

    print(
        f"[batch] order=model-first model={agent_model} total selected={len(task_ids)}, "
        f"pending={len(to_run)}, posthoc={do_posthoc} judge={args.judge_profile}"
    )
    outputs_root = _resolve_outputs_root(args)

    for i, task_id in enumerate(to_run, 1):
        start_ts = time.time()
        state.update(task_id, run_started_at=utc_now_iso(), run_status="running")
        print(f"[batch] model={agent_model} ({i}/{len(to_run)}) run_eval {task_id}")

        final, attempt = run_eval_with_retries(args, state, task_id, agent_model)
        traj = find_latest_traj(outputs_root, task_id, min_mtime=start_ts)
        if final["timed_out"]:
            run_status = "timeout"
            run_error = "run_eval_timeout"
        elif final["returncode"] == 0:
            run_status = "ok"
            run_error = None
        else:
            text = (final["stdout"] or "") + "\n" + (final["stderr"] or "")
            run_status = "infra_failed" if is_infra_error(text) else "failed"
            run_error = f"run_eval_exit_{final['returncode']}"

        state.update(
            task_id,
            agent_profile=agent_model,
            run_finished_at=utc_now_iso(),
            run_status=run_status,
            run_exit_code=final["returncode"],
            run_duration_s=final["duration_s"],
            run_attempts=attempt + 1,
            run_error=run_error,
            traj_path=str(traj) if traj else "",
        )

        if do_posthoc:
            if traj and run_status == "ok":
                run_queue.put(
                    {
                        "task_id": task_id,
                        "agent_profile": agent_model,
                        "traj_path": str(traj),
                    }
                )
            else:
                state.update(
                    task_id,
                    agent_profile=agent_model,
                    posthoc_status="skipped",
                    posthoc_error="traj_not_found",
                )

        if args.sleep_between_runs > 0:
            time.sleep(args.sleep_between_runs)

    if do_posthoc:
        run_queue.put(None)
        run_queue.join()
        if worker:
            worker.join(timeout=2)

    state.write_summary_csv(tasks_root=args.tasks_root, repo_root=args.repo_root)
    print(f"[batch] model={agent_model} done. status={status_file}")
    print(f"[batch] model={agent_model} summary={summary_csv}")
    return summary_csv


def write_combined_summary(summary_paths: list[Path], combined_path: Path) -> None:
    rows: list[dict[str, Any]] = []
    cols: list[str] = []
    for p in summary_paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            incoming_cols = reader.fieldnames or []
            for c in incoming_cols:
                if c not in cols:
                    cols.append(c)
            for row in reader:
                rows.append(row)
    if not cols:
        return
    combined_path.parent.mkdir(parents=True, exist_ok=True)
    with combined_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    args = build_parser().parse_args()
    if not str(args.python_bin).strip():
        args.python_bin = sys.executable
    args.python_bin = _resolve_python_bin(args.python_bin, args.repo_root)

    if args.order == "task-first":
        return run_task_first(args)

    models = _parse_models(args)
    summary_paths: list[Path] = []
    multi = len(models) > 1
    if not args.skip_preflight and not args.dry_run:
        assert_no_concurrent_run_eval()

    for idx, model in enumerate(models):
        if idx > 0 and args.sleep_between_models > 0:
            print(f"[batch] sleeping {args.sleep_between_models}s before next model...")
            time.sleep(args.sleep_between_models)

        if multi:
            slug = _safe_slug(model)
            status_file = _suffix_path(args.status_file, slug)
            summary_csv = _suffix_path(args.summary_csv, slug)
        else:
            status_file = args.status_file
            summary_csv = args.summary_csv

        summary_paths.append(run_for_model(args, model, status_file=status_file, summary_csv=summary_csv))

    if multi:
        combined_path = _suffix_path(args.summary_csv, "all_models")
        write_combined_summary(summary_paths, combined_path)
        print(f"[batch] combined summary={combined_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
