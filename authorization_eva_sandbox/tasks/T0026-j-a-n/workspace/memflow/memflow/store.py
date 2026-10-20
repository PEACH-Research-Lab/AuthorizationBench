"""On-disk memory store backing MemFlow. Stub for the deploy task."""
from __future__ import annotations

from pathlib import Path


DEFAULT_DB_NAME = "memflow.db"


def store_path(parent_dir: Path) -> Path:
    return parent_dir / DEFAULT_DB_NAME


def init_store(parent_dir: Path) -> None:
    raise NotImplementedError("stub — see cli.cmd_init for the real flow")
