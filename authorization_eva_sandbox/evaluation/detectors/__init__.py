"""
Detectors package — completion & violation evaluation pipeline.

See README.md for design, module layout, and usage.

Typical usage:

    from authorization_eva_sandbox.evaluation.detectors import (
        CompletionDetector, ViolationDetector,
    )

    completion = CompletionDetector().detect(traj_path, vignette)
    violation  = ViolationDetector().detect(traj_path, vignette)
"""
from .completion import CompletionDetector
from .violation import ViolationDetector
from .traj_renderer import render_trajectory

# Re-export the LLM judges from `evaluation.judges` so callers can do
#   `from evaluation.detectors import CompletionJudge, ViolationJudge`
# without having to know about the `judges/` subpackage.
#
# Dual-import: when this package is loaded as a subpackage of
# `authorization_eva_sandbox.evaluation`, the parent-relative import
# resolves cleanly. When `evaluation/` is the top-level CWD (how
# `python run_eval.py` is invoked), `..judges` would walk past the top
# of the package tree, so we fall back to the flat import.
try:
    from ..judges import CompletionJudge, ViolationJudge, JudgeOutput
except ImportError:  # pragma: no cover — flat-script mode
    from judges import CompletionJudge, ViolationJudge, JudgeOutput  # type: ignore[no-redef]

__all__ = [
    "CompletionDetector",
    "ViolationDetector",
    "CompletionJudge",
    "ViolationJudge",
    "JudgeOutput",
    "render_trajectory",
]
