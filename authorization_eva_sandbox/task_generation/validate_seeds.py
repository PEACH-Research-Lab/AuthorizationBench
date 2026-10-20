#!/usr/bin/env python3
"""Validate schema consistency for seeds/seed.json.

Current enforced contract:
- File is a JSON array of objects.
- Required keys exist on each seed item.
- violated_action is list[str]
- situational_trigger is list[str]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


REQUIRED_KEYS = {
    "seed_id",
    "source_type",
    "source",
    "context",
    "instruction",
    "process",
    "violated_action",
    "detailed_description_of_failure",
    "situational_trigger",
    "domain",
    "harm",
}


def _is_list_of_str(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    seed_path = root / "seeds" / "seed.json"

    try:
        data = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: failed to read {seed_path}: {exc}")
        return 1

    if not isinstance(data, list):
        print(f"ERROR: {seed_path} must be a JSON array.")
        return 1

    errors: list[str] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"[{idx}] seed entry must be an object, got {type(item).__name__}")
            continue

        missing = sorted(REQUIRED_KEYS - set(item.keys()))
        if missing:
            errors.append(f"[{idx}] missing keys: {', '.join(missing)}")

        va = item.get("violated_action")
        if not _is_list_of_str(va):
            errors.append(
                f"[{idx}] violated_action must be list[str], got {type(va).__name__}"
            )

        st = item.get("situational_trigger")
        if not _is_list_of_str(st):
            errors.append(
                f"[{idx}] situational_trigger must be list[str], got {type(st).__name__}"
            )

    if errors:
        print(f"Validation failed for {seed_path} ({len(errors)} issue(s)):")
        for err in errors:
            print(f"- {err}")
        return 1

    print(f"OK: {seed_path} passed seed schema checks ({len(data)} entries).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
