"""Canonical V4 Stage 2b entrypoint: certified Story to structured Script.

The implementation currently shares validation primitives with ``stage2``;
this module is the only supported command entrypoint for Stage 2b.
"""
from __future__ import annotations

from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
    _step2_main,
)


def main() -> int:
    return _step2_main()


if __name__ == "__main__":
    raise SystemExit(main())
