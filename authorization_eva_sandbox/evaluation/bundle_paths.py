"""Resolve active task bundle directories (v3 pipeline output only).

Legacy Flask/v1 bundles live under ``legacy_archive/`` and are not searched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

_SANDBOX_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = _SANDBOX_ROOT / "tasks"


def resolve_task_bundle_dir(task_id: str) -> Optional[Path]:
    """Return ``tasks/<task_id>`` when it exists and contains ``task_config.json``."""
    if not task_id:
        return None
    bundle = TASKS_DIR / str(task_id)
    if (bundle / "task_config.json").is_file():
        return bundle
    return None


def load_task_config_for_eval(task_id: str) -> tuple[Optional[dict[str, Any]], Optional[Path]]:
    bundle = resolve_task_bundle_dir(task_id)
    if bundle is None:
        return None, None
    with open(bundle / "task_config.json", encoding="utf-8") as f:
        return json.load(f), bundle
