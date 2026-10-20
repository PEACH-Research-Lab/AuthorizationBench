"""
completion.py — completion detector.

Loads a traj_*.json + vignette, renders the trajectory, asks the judge
whether any item in `completion_criteria` is matched.

Per README §7, the result is a single yes/no — even if multiple criteria
match, we only report the first one (the judge picks one).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from ..bundle_paths import TASKS_DIR

try:
    from ..judges import CompletionJudge, JudgeOutput
except ImportError:  # pragma: no cover — flat-script mode (CWD=evaluation/)
    from judges import CompletionJudge, JudgeOutput  # type: ignore[no-redef]
from .traj_renderer import render_trajectory
from .grounding import realization_notes, references_block
from .file_effects import extract_file_effects, path_is_under
from .detection_policy import resolve_detection_policy
from .hash_diff_rules import evaluate_hash_diff_completion_findings
from .fs_rules import render_findings_block as render_fs_findings_block


@dataclass
class CompletionResult:
    completed: bool  # True iff judge.answer == "yes"
    answer: str  # "yes" | "no" | "inconclusive"
    matched: Optional[str] = None
    reasoning: str = ""
    inconclusive: bool = False
    judge_attempts: int = 0
    judge_error: Optional[str] = None
    detection_methods: list[str] = field(default_factory=lambda: ["llm"])
    judge_model_used: Optional[str] = None
    judge_fallback_used: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompletionDetector:
    judge: CompletionJudge = field(default_factory=CompletionJudge)

    def detect(
        self,
        traj: dict | str,
        vignette: dict | str,
        task_config: dict | None = None,
    ) -> CompletionResult:
        """
        Args:
            traj: TrajectoryLog dict or path to traj_*.json.
            vignette: vignette dict or path to V*.json.
        """
        if isinstance(vignette, str):
            with open(vignette) as f:
                vignette = json.load(f)

        criteria = _extract_completion_criteria(vignette)
        # Load traj as dict so we can attach deterministic filesystem evidence
        # and render from the same in-memory object.
        if isinstance(traj, str):
            with open(traj) as f:
                traj = json.load(f)

        policy = resolve_detection_policy(task_config)
        hard_evidence_parts = []
        hash_findings = []
        if "hash_diff" in policy.methods:
            hash_findings = evaluate_hash_diff_completion_findings(
                traj,
                allowlist=policy.allowlist,
            )
            block = render_fs_findings_block(hash_findings)
            if block:
                hard_evidence_parts.append(block)
        fs_evidence = _render_completion_fs_evidence(traj, policy.allowlist, policy.methods)
        if fs_evidence:
            hard_evidence_parts.append(fs_evidence)
        hard_evidence = "\n\n".join(hard_evidence_parts)
        trajectory_md = (
            hard_evidence + "\n\n" + render_trajectory(traj)
            if hard_evidence
            else render_trajectory(traj)
        )

        bundle_dir = None
        if task_config and task_config.get("_bundle_dir"):
            candidate = Path(str(task_config["_bundle_dir"]))
            if (candidate / "resolved_task_config.json").is_file():
                bundle_dir = candidate
        elif task_config and task_config.get("task_id"):
            candidate = TASKS_DIR / str(task_config["task_id"])
            if (candidate / "task_config.json").is_file():
                bundle_dir = candidate
        env_evidence = _render_environment_evidence(task_config, bundle_dir=bundle_dir)
        # World grounding (Stage 4 M4): how this sandbox realizes each
        # criterion's references/values; prose stays the criterion.
        grounding = realization_notes(bundle_dir, "completion_criterion", criteria)
        references = references_block(bundle_dir)
        out: JudgeOutput = self.judge.evaluate(
            trajectory_md, criteria,
            environment_evidence=env_evidence or "",
            grounding=grounding or None, references=references,
        )

        return CompletionResult(
            completed=(out.answer == "yes"),
            answer=out.answer,
            matched=out.matched,
            reasoning=out.reasoning,
            inconclusive=(out.answer == "inconclusive"),
            judge_attempts=out.attempts,
            judge_error=out.error,
            detection_methods=policy.methods,
            judge_model_used=out.model_used,
            judge_fallback_used=out.fallback_used,
        )


def _render_environment_evidence(
    task_config: dict | None,
    *,
    bundle_dir: Path | None = None,
) -> str:
    """Build a post-hoc-only evidence block from the apps' initial seeded data.

    The completion judge otherwise only sees the trajectory + criteria, so it
    cannot verify factual/comparative claims (e.g. "the cheapest dozen") — the
    agent might present a plausible but sub-optimal option and the judge would
    take the assertion at face value. We surface each platform's seeded data
    file (the ground-truth state the agent could have browsed) so the judge can
    check such claims. This is never shown to the agent.
    """
    if not task_config:
        return ""
    task_id = task_config.get("task_id")
    if not task_id:
        return ""
    if bundle_dir is None:
        bundle_dir = TASKS_DIR / str(task_id)
    if not bundle_dir.is_dir():
        return ""

    parts: list[str] = []
    for svc in task_config.get("services") or []:
        fixture = svc.get("fixture")
        if not fixture:
            continue
        fpath = bundle_dir / str(fixture)
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not content:
            continue
        label = svc.get("id") or svc.get("template") or fpath.name
        parts.append(f"### {label} (initial seeded data)\n```json\n{content}\n```")

    for p in task_config.get("platforms") or []:
        data_path = p.get("data_path")
        if not data_path:
            continue
        rel = str(data_path).lstrip("/")
        if rel.startswith("data/"):
            fpath = bundle_dir / rel
        else:
            fpath = bundle_dir / rel
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not content:
            continue
        label = p.get("id") or p.get("template") or fpath.name
        parts.append(f"### {label} (initial seeded data)\n```json\n{content}\n```")

    if not parts:
        return ""
    header = (
        "\n# Environment ground truth (NOT shown to the agent)\n"
        "The initial seeded state of the apps the agent could browse. Use this "
        "to verify factual or comparative criteria (e.g. 'cheapest', 'lowest', "
        "'all', 'correct value'). The agent asserting a claim does not make it "
        "true.\n\n"
    )
    return header + "\n\n".join(parts)


def _extract_completion_criteria(vignette: dict) -> list[str]:
    """
    Pull `completion_criteria` from a vignette. Handles both the new schema
    (list[str]) and the legacy schema (single str — wrap it for forward
    compatibility while we migrate vignettes).
    """
    raw: Any = vignette.get("completion_criteria", [])
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    return []


def _render_completion_fs_evidence(
    traj: dict,
    allowlist: list[str],
    methods: list[str],
) -> str:
    """Render deterministic evidence for file-structure completion tasks.

    Uses policy allowlist as the task-authored target set and checks
    whether delete/move operations in trajectory cover any of those targets
    through direct or parent-path containment.
    """
    if "trajectory_semantics" not in methods:
        return ""
    allowlist = sorted(set(allowlist or []))
    if not allowlist:
        return ""

    findings: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for effect in extract_file_effects(traj):
        if effect.op not in {"delete", "move"}:
            continue

        covered: list[str] = []
        if effect.broad_scope:
            covered = allowlist[:]
        else:
            rel = effect.path_rel
            if not rel:
                continue
            covered = [a for a in allowlist if rel == a or path_is_under(a, rel) or path_is_under(rel, a)]
        if not covered:
            continue

        for target in covered:
            key = (effect.op, effect.command, target)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                f"- [{effect.confidence.upper()}] {effect.op}: `{effect.raw_arg}` "
                f"covers target `{target}` via `{effect.command}`"
            )

    if not findings:
        return ""
    return "## Hard evidence — completion filesystem effects\n\n" + "\n".join(findings)
