"""Plan and immediately build the trigger-flip batch."""
from __future__ import annotations

from authorization_eva_sandbox.task_generation.expansion.trigger_flip_build import (
    main as build_main,
)
from authorization_eva_sandbox.task_generation.expansion.trigger_flip_plan import (
    generate_plans,
)


def main() -> int:
    rows, planning_failed = generate_plans()
    confident = [row for row in rows if row.get("applicable") is True]
    waiting = [
        row.get("source_task")
        for row in rows
        if row.get("applicable") is not True
    ]
    print(
        f"planning complete: {len(confident)} confident, "
        f"{len(waiting)} waiting",
        flush=True,
    )
    if waiting:
        print("waiting: " + ", ".join(str(item) for item in waiting), flush=True)
    build_failed = build_main()
    return 1 if planning_failed or build_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

