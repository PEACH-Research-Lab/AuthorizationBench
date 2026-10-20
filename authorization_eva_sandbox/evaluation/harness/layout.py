"""Run directory and post-hoc report locations.

OpenHands keeps the historical layout, one directory under the outputs root:

    outputs/<setting>/output_full_<task>_<setting>_<n>/
    post_hoc_eval_report/openhands-native/<setting>/<run dir>.md
    post_hoc_eval_report/openhands-sealed/<setting>/<run dir>.md

OpenHands native and sealed reports are separate trees. A trajectory with
no prompt condition is sealed. Any other harness adds one directory and its
name in the run directory, so two harnesses can run the same task and
setting without sharing a folder:

    outputs/<harness>/<setting>/output_full_<task>_<setting>_<harness>_<n>/
    post_hoc_eval_report/<harness>/<setting>/<run dir>.md
"""
from __future__ import annotations

import json
import os
from pathlib import Path

OPENHANDS_HARNESS = "openhands"
OPENHANDS_NATIVE_DIR = "openhands-native"
OPENHANDS_SEALED_DIR = "openhands-sealed"


def _model_short_name(model: str) -> str:
    """Extract a short, filesystem-safe model name.

    e.g. 'openrouter/google/gemini-2.5-pro' -> 'gemini-2.5-pro'
         'openai/gpt-4o'                    -> 'gpt-4o'
         'deepseek/deepseek-chat-v3-0324'   -> 'deepseek-chat-v3-0324'
    """
    short = model.rsplit("/", 1)[-1]
    return short.replace(" ", "_")


def _resolve_outputs_path(
    base_dir: str,
    task_short_name: str,
    model: str,
    reasoning_effort: str | None = None,
    harness: str = OPENHANDS_HARNESS,
) -> str:
    """Claim ``{base}/{setting}/output_full_{task}_{setting}_{n}``.

    ``harness`` other than openhands claims
    ``{base}/{harness}/{setting}/output_full_{task}_{setting}_{harness}_{n}``.
    The directory is created with ``os.mkdir``; a lost race takes the next number.
    """
    model_tag = _model_short_name(model)
    if reasoning_effort:
        model_tag = f"{model_tag}-{reasoning_effort}"
    if harness and harness != OPENHANDS_HARNESS:
        model_dir = os.path.join(base_dir, harness, model_tag)
        base_name = f"output_full_{task_short_name}_{model_tag}_{harness}"
    else:
        model_dir = os.path.join(base_dir, model_tag)
        base_name = f"output_full_{task_short_name}_{model_tag}"
    os.makedirs(model_dir, exist_ok=True)
    run_num = 1
    while True:
        candidate = os.path.join(model_dir, f"{base_name}_{run_num}")
        try:
            os.mkdir(candidate)
        except FileExistsError:
            run_num += 1
            continue
        return candidate


def harness_from_run_dir(run_dir: Path) -> str | None:
    """Harness directory above the setting, or None for an OpenHands run.

    An OpenHands run is ``<outputs>/<setting>/<output_full_task_setting_n>``.
    Another harness is ``<outputs>/<harness>/<setting>/<output_full_task_setting_harness_n>``.
    """
    setting = run_dir.parent.name
    candidate = run_dir.parent.parent.name
    if not setting or not candidate or candidate == OPENHANDS_HARNESS:
        return None
    name = run_dir.name
    marker = f"_{setting}_{candidate}_"
    tail = name.rsplit("_", 1)[-1]
    if marker in name and tail.isdigit():
        return candidate
    return None


def prompt_condition_of_trajectory(traj_path: Path) -> str:
    """``native`` or ``sealed``. A missing file or an older trajectory is sealed."""
    try:
        metadata = json.loads(Path(traj_path).read_text(encoding="utf-8")).get("metadata")
    except (OSError, json.JSONDecodeError):
        return "sealed"
    prompt = metadata.get("prompt") if isinstance(metadata, dict) else None
    if isinstance(prompt, dict) and prompt.get("condition") == "native":
        return "native"
    return "sealed"


def report_dir_for_trajectory(
    report_root: Path,
    traj_path: Path,
    *,
    setting_fallback: str,
) -> Path:
    """Directory for this trajectory's post-hoc report.

    ``setting_fallback`` is used only when the trajectory has no setting directory.
    OpenHands reports go under ``openhands-native`` or ``openhands-sealed``.
    """
    run_dir = Path(traj_path).parent
    setting = run_dir.parent.name or setting_fallback
    harness = harness_from_run_dir(run_dir)
    if harness:
        return Path(report_root) / harness / setting
    bucket = (
        OPENHANDS_NATIVE_DIR
        if prompt_condition_of_trajectory(traj_path) == "native"
        else OPENHANDS_SEALED_DIR
    )
    return Path(report_root) / bucket / setting
