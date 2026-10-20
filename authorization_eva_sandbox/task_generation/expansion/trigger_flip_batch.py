"""Four-worker queue for trigger-flip planning and building."""
from __future__ import annotations

import argparse
import fcntl
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from authorization_eva_sandbox.task_generation.expansion.trigger_flip import (
    TARGET_BY_SOURCE,
    destination_task_id,
)
from authorization_eva_sandbox.task_generation.expansion.trigger_flip_build import (
    TASKS,
    build_and_install,
)
from authorization_eva_sandbox.task_generation.expansion.trigger_flip_plan import (
    SANDBOX,
    plan_task,
)

BATCH = SANDBOX / "outputs" / "_trigger_flip" / "batch"
QUEUE = BATCH / "queue.txt"
QUEUE_LOCK = BATCH / "queue.lock"
DONE = BATCH / "done.jsonl"
DONE_LOCK = BATCH / "done.lock"
PLANS = BATCH / "plans"
REVIEWS = BATCH / "reviews"
COMPLETE = BATCH / "COMPLETE"


def eligible_tasks() -> list[str]:
    tasks = []
    for path in sorted(TASKS.iterdir()):
        parts = path.name.split("-")
        if (
            path.is_dir()
            and len(parts) == 4
            and parts[1] in TARGET_BY_SOURCE
            and not (TASKS / destination_task_id(path.name)).exists()
        ):
            tasks.append(path.name)
    return tasks


def prepare(*, restart: bool = False) -> list[str]:
    BATCH.mkdir(parents=True, exist_ok=True)
    PLANS.mkdir(exist_ok=True)
    REVIEWS.mkdir(exist_ok=True)
    tasks = eligible_tasks()
    if restart or not QUEUE.exists():
        QUEUE.write_text("".join(task + "\n" for task in tasks), encoding="utf-8")
        DONE.write_text("", encoding="utf-8")
        COMPLETE.unlink(missing_ok=True)
    return tasks


def worker(slot: int) -> int:
    BATCH.mkdir(parents=True, exist_ok=True)
    failed = False
    while True:
        task = _pop()
        if task is None:
            print(f"slot {slot} queue empty", flush=True)
            return 1 if failed else 0
        print(f"slot {slot} planning {task}", flush=True)
        try:
            plan = plan_task(task)
            plan_path = PLANS / f"{task}.json"
            plan_path.write_text(
                json.dumps(asdict(plan), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if not plan.applicable:
                _record(slot, task, "waiting", plan=plan_path)
                print(f"slot {slot} waiting {task}", flush=True)
                continue
            print(f"slot {slot} planned {task}; pausing 2s", flush=True)
            time.sleep(2)
            installed = build_and_install(plan)
        except Exception as exc:
            failed = True
            _record(
                slot,
                task,
                "error",
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
            print(
                f"slot {slot} error {task}: {type(exc).__name__}: {exc}",
                flush=True,
            )
        else:
            _record(slot, task, "installed", destination=installed)
            print(f"slot {slot} installed {installed.name}", flush=True)


def mark_complete() -> None:
    COMPLETE.write_text("workers finished\n", encoding="utf-8")


def _pop() -> str | None:
    with QUEUE_LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = QUEUE.read_text(encoding="utf-8").splitlines()
        if not lines:
            return None
        task = lines.pop(0)
        QUEUE.write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )
        return task


def _record(
    slot: int,
    source: str,
    status: str,
    *,
    plan: Path | None = None,
    destination: Path | None = None,
    error: str | None = None,
) -> None:
    row: dict[str, Any] = {
        "time": time.time(),
        "slot": slot,
        "source": source,
        "destination": destination.name if destination else destination_task_id(source),
        "status": status,
    }
    if plan:
        row["plan"] = str(plan)
    if error:
        row["error"] = error
    with DONE_LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with DONE.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--restart", action="store_true")
    run = sub.add_parser("worker")
    run.add_argument("--slot", required=True, type=int)
    sub.add_parser("complete")
    args = parser.parse_args()
    if args.command == "prepare":
        tasks = prepare(restart=args.restart)
        print(f"{len(tasks)} tasks -> {QUEUE}")
        return 0
    if args.command == "worker":
        return worker(args.slot)
    mark_complete()
    print(COMPLETE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

