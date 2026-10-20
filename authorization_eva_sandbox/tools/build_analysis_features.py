#!/usr/bin/env python3
"""Backfill turn-level metrics into a results CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from analysis_feature_utils import (
    TURN_LEVEL_FIELDS,
    compute_turn_metrics_from_traj,
    resolve_output_file_to_abs,
)


def infer_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    repo_root = infer_repo_root()
    default_csv = (
        repo_root
        / "authorization_eva_sandbox"
        / "evaluation"
        / "outputs"
        / "batch_status_dual_models_all_models.csv"
    )
    p = argparse.ArgumentParser(description="Augment result CSV with turn-level metrics")
    p.add_argument("--repo-root", type=Path, default=repo_root, help="Repository root")
    p.add_argument("--input-csv", type=Path, default=default_csv, help="Input results CSV")
    p.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Output CSV path (default: overwrite input)",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    input_csv = args.input_csv
    output_csv = args.output_csv or input_csv
    if not input_csv.exists():
        raise SystemExit(f"input csv not found: {input_csv}")

    with input_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])

    if not rows:
        print("[features] no rows found; nothing to do")
        return 0

    for col in TURN_LEVEL_FIELDS:
        if col not in fields:
            fields.append(col)

    for row in rows:
        traj_abs = resolve_output_file_to_abs(row.get("output_file", ""), repo_root=args.repo_root)
        metrics = compute_turn_metrics_from_traj(traj_abs)
        for k, v in metrics.items():
            row[k] = v

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"[features] updated_rows={len(rows)} output={output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
