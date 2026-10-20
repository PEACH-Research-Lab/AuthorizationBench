#!/usr/bin/env python3
"""Package AuthorizationBench trajectories and verdicts for Hugging Face."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import shutil
from collections import defaultdict
from pathlib import Path


def _run_directory(runs_root: Path, row: dict[str, str]) -> Path:
    parent = runs_root / row["harness"] / row["setting"]
    prefix = f"output_full_{row['task']}_{row['setting']}_"
    candidates = [
        path
        for path in parent.glob(f"{prefix}*")
        if path.is_dir() and path.name.rsplit("_", 1)[-1] == row["run"]
    ]
    if len(candidates) != 1:
        raise FileNotFoundError(
            f"expected one run directory for {row['harness']}/{row['setting']}/"
            f"{row['task']}/{row['run']}, found {candidates}"
        )
    return candidates[0]


def _read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build(release_root: Path, runs_root: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    (destination / "data").mkdir(parents=True)

    card = release_root / "huggingface" / "README.md"
    shutil.copy2(card, destination / "README.md")
    shutil.copy2(release_root / "LICENSE", destination / "LICENSE")
    shutil.copy2(runs_root / "index.csv", destination / "index.csv")
    shutil.copy2(
        release_root / "benchmark" / "manifest.json",
        destination / "benchmark_manifest.json",
    )
    shutil.copy2(
        release_root / "benchmark" / "lineage.csv",
        destination / "task_lineage.csv",
    )
    shutil.copytree(
        release_root / "results" / "paper",
        destination / "paper_results",
    )

    with (runs_root / "index.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 6864:
        raise ValueError(f"expected 6864 run rows, found {len(rows)}")

    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["harness"], row["setting"])].append(row)

    trajectory_count = 0
    verdict_count = 0
    for (harness, setting), group in sorted(grouped.items()):
        shard_dir = destination / "data" / harness
        shard_dir.mkdir(parents=True, exist_ok=True)
        shard_path = shard_dir / f"{setting}.jsonl.gz"
        with gzip.open(shard_path, "wt", encoding="utf-8") as output:
            for row in group:
                run_dir = _run_directory(runs_root, row)
                report = _read_json(run_dir / "report.json")
                if report is None:
                    raise FileNotFoundError(run_dir / "report.json")
                verdict_count += 1

                trajectory_paths = sorted(run_dir.glob("traj_*.json"))
                trajectory = _read_json(trajectory_paths[0]) if trajectory_paths else None
                if trajectory is not None:
                    trajectory_count += 1

                record = {
                    **row,
                    "run_id": f"{harness}/{setting}/{row['task']}/{row['run']}",
                    "trajectory_available": trajectory is not None,
                    "report": report,
                    "trajectory": trajectory,
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")

    metadata = {
        "schema_version": 1,
        "records": len(rows),
        "verdicts": verdict_count,
        "trajectories": trajectory_count,
        "missing_trajectories": len(rows) - trajectory_count,
        "harnesses": sorted({row["harness"] for row in rows}),
        "settings": sorted({row["setting"] for row in rows}),
    }
    (destination / "dataset_manifest.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    build(
        args.release_root.resolve(),
        args.runs_root.resolve(),
        args.destination.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
