"""Smoke driver for the builtin loop against a live v2 container.

Usage:
    ./au312/bin/python evaluation/scripts/smoke_builtin_loop.py \
        --task-config /tmp/v2_smoke/config/task_config.json \
        --env-url http://127.0.0.1:19100 --container auth-v2-smoke \
        --run-dir /tmp/v2_smoke_run --model-profile gemini_2_5_flash \
        --max-steps 12
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(EVAL_DIR.parent))

from harness.base import AgentTaskView, GraderEnvironmentClient, RunSpec  # noqa: E402
from harness.builtin_loop import BuiltinLoopAdapter  # noqa: E402


def load_profile(name: str) -> dict:
    cfg = tomllib.loads((EVAL_DIR / "config.toml").read_text())
    profile = cfg.get("llm", {}).get(name)
    if profile is None:
        raise SystemExit(f"profile [llm.{name}] not found in evaluation/config.toml")
    return profile


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-config", required=True)
    ap.add_argument("--env-url", default="http://127.0.0.1:19100")
    ap.add_argument("--container", required=True)
    ap.add_argument("--grader-token", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--model-profile", default="gemini_2_5_flash")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--no-native-tools", action="store_true")
    args = ap.parse_args()

    task_config = json.loads(Path(args.task_config).read_text())
    profile = load_profile(args.model_profile)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    spec = RunSpec(
        task_id=task_config.get("task_id", "SMOKE"),
        task_view=AgentTaskView.from_task_config(task_config),
        run_dir=str(run_dir),
        env_base_url=args.env_url,
        container_name=args.container,
        grader=GraderEnvironmentClient(args.env_url, args.grader_token),
        model=profile["model"],
        api_key=profile.get("api_key"),
        base_url=profile.get("base_url"),
        max_steps=args.max_steps,
        condenser={"max_size": 40, "keep_first": 1},
        native_tools=not args.no_native_tools,
    )

    adapter = BuiltinLoopAdapter()
    result = adapter.run(spec)

    traj = result.trajectory_log
    traj_path = run_dir / f"traj_{spec.task_id}.json"
    traj.save(str(traj_path))

    print("\n" + "=" * 70)
    print(f"stop_reason      : {result.stop_reason}")
    print(f"final_message    : {result.final_message!r}")
    print(f"error            : {result.error}")
    print(f"total steps      : {len(traj.steps)}")
    print(f"cost             : {traj.metadata.get('cost')}")
    print(f"traj saved       : {traj_path}")
    print("=" * 70)
    for s in traj.steps:
        head = str(s.data)[:140].replace("\n", " ")
        print(f"  [{s.step:>2} {s.type:<17}] {head}")


if __name__ == "__main__":
    main()
