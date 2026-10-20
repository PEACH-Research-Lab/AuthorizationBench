"""Canonical V4 Stage 2c entrypoint: certified Script to Surface plan.

The implementation currently shares validation primitives with ``stage2``;
this module is the only supported command entrypoint for Stage 2c.
"""
from __future__ import annotations

from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
    _step3_main,
)


def main() -> int:
    return _step3_main()


if __name__ == "__main__":
    raise SystemExit(main())
