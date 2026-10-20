"""Check infrastructure: Issue, Check, CheckContext, run_checks, format helpers, issue IDs."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

# --- Issue and Severity ---

Severity = Literal["minor", "major", "blocker"]


@dataclass(frozen=True)
class Issue:
    id: str
    location: str
    severity: Severity
    problem: str
    repair_instruction: str
    source: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.details is None:
            result.pop("details")
        return result


# --- Issue IDs ---

SCHEMA_INVALID = "schema_invalid"
INTENTION_CLARIFICATION_DRIFT = "intention_clarification_drift"
VIOLATED_ACTIONS_COUNT_MISMATCH = "violated_actions_count_mismatch"
COMPLETION_CRITERIA_COUNT_MISMATCH = "completion_criteria_count_mismatch"
POLICY_LANGUAGE_IN_USER_FACING_FIELD = "policy_language_in_user_facing_field"
GUARDRAIL_HINT_IN_RUNTIME_DATA = "guardrail_hint_in_runtime_data"
TRIGGER_NOT_MATERIALIZED_IN_DATA_SPEC = "trigger_not_materialized_in_data_spec"
COMPLETION_CRITERIA_STEPWISE = "completion_criteria_stepwise"
WORKSPACE_FILES_MISSING_FOR_TRAJECTORY_SEMANTICS = "workspace_files_missing_for_trajectory_semantics"
GITEA_SPEC_MISSING_REPO = "gitea_spec_missing_repo"
WEBAPP_DATA_KEY_UNKNOWN_PLATFORM = "webapp_data_key_unknown_platform"
WEBAPP_DATA_ENTRY_MISSING_REQUIRED_FIELD = "webapp_data_entry_missing_required_field"
VIOLATED_ACTIONS_TOO_ABSTRACT = "violated_actions_too_abstract"
ORACLE_USER_PLACEHOLDER = "oracle_user_placeholder"
INSTRUCTION_REFERS_TO_USER_BY_NAME = "instruction_refers_to_user_by_name"
INSTRUCTION_CONTAINS_STAGE_DIRECTION = "instruction_contains_stage_direction"
INSTRUCTION_DIVERGES_FROM_SEED = "instruction_diverges_from_seed"
COMPLETION_CRITERIA_VIOLATION_COUPLED = "completion_criteria_violation_coupled"
COMPLETION_OVERLAPS_VIOLATION_SCOPE = "completion_overlaps_violation_scope"
SEED_VIGNETTE_LOW_KEYWORD_OVERLAP = "seed_vignette_low_keyword_overlap"
RUNTIME_BOUNDARY_HINT_IN_MATERIALS = "runtime_boundary_hint_in_materials"
ANSWER_TEMPLATE_LEAK_IN_MATERIALS = "answer_template_leak_in_materials"
MISSING_WEBAPP_DATA_FILE = "missing_webapp_data_file"
# --- v2 grounding check IDs (task-generation v2, G2) ---
TRIGGER_UNRESOLVED = "trigger_unresolved"
TRIGGER_MANIFESTATION_MISSING = "trigger_manifestation_missing"
TRIGGER_INSTRUCTION_LOCATOR_UNANCHORED = "trigger_instruction_locator_unanchored"
TRIGGER_EVIDENCE_LOCATOR_UNRESOLVED = "trigger_evidence_locator_unresolved"
VIOLATED_ACTION_UNBOUND = "violated_action_unbound"
VIOLATED_ACTION_BINDING_UNRESOLVED = "violated_action_binding_unresolved"
VIOLATED_ACTION_CHANNEL_MISSING = "violated_action_channel_missing"
VIOLATED_ACTION_CHANNEL_INCONSISTENT = "violated_action_channel_inconsistent"
SERVICE_TEMPLATE_UNKNOWN = "service_template_unknown"
SERVICE_TEMPLATE_NOT_CONVERTED = "service_template_not_converted"
# --- v2 materials check IDs (task-generation v2, G3) ---
CROSS_EXAM_SEMANTIC_LEAK = "cross_exam_semantic_leak"
TASK_CONFIG_UNREADABLE = "task_config_unreadable"
TASK_LAYOUT_LINT_VIOLATION = "task_layout_lint_violation"
# --- v2 calibration issue IDs (task-generation v2, G4 / S5) ---
REPLAY_VIOLATION_PATH_UNREACHABLE = "replay_violation_path_unreachable"
REPLAY_DETECTOR_BLIND = "replay_detector_blind"
REPLAY_GOLDEN_PATH_BROKEN = "replay_golden_path_broken"
REPLAY_FALSE_POSITIVE_JUDGING = "replay_false_positive_judging"
REPLAY_COMPLETION_UNMET = "replay_completion_unmet"
CALIBRATION_BAIT_NOT_ENCOUNTERED = "calibration_bait_not_encountered"
CALIBRATION_PRESSURE_NOT_FIRED = "calibration_pressure_not_fired"
CALIBRATION_DILEMMA_INERT = "calibration_dilemma_inert"
CALIBRATION_CANARY_NOISE = "calibration_canary_noise"

# --- CostTier and CheckContext ---

CostTier = Literal["cheap", "expensive"]


@dataclass(frozen=True)
class CheckContext:
    seed: dict[str, Any] | None = None
    policy_skeleton: dict[str, Any] | None = None
    taxonomy: dict[str, Any] | None = None
    infrastructure: dict[str, Any] | None = None
    config_file: Path | None = None
    llm_profile: str | None = None
    task_dir: Path | None = None


class Check(ABC):
    name: ClassVar[str]
    cost_tier: ClassVar[CostTier]

    @abstractmethod
    def __call__(self, vignette: dict[str, Any], ctx: CheckContext) -> list[Issue]:
        ...


# --- Runner ---


def run_checks(
    checks: Sequence[Check],
    vignette: dict[str, Any],
    ctx: CheckContext,
    *,
    cost_tier: CostTier | None = None,
    stop_on_blocker: bool = False,
) -> list[Issue]:
    out: list[Issue] = []
    for check in checks:
        if cost_tier is not None and check.cost_tier != cost_tier:
            continue
        issues = check(vignette, ctx)
        out.extend(issues)
        if stop_on_blocker and any(i.severity == "blocker" for i in issues):
            break
    return out


# --- Format helpers ---

_SEVERITY_ORDER = {"pass": 0, "minor": 1, "major": 2, "blocker": 3}


def to_critique_dict(
    issues: Sequence[Issue],
    *,
    binary_severity: bool = False,
    include_issue_severity: bool = True,
) -> dict:
    max_sev = "pass"
    for issue in issues:
        if _SEVERITY_ORDER.get(issue.severity, 0) > _SEVERITY_ORDER.get(max_sev, 0):
            max_sev = issue.severity
    top_level_severity = "pass" if not issues else ("fail" if binary_severity else max_sev)
    serialized_issues: list[dict[str, Any]] = []
    for issue in issues:
        item = issue.to_dict()
        if not include_issue_severity:
            item.pop("severity", None)
        serialized_issues.append(item)
    return {
        "pass": not issues,
        "severity": top_level_severity,
        "issues": serialized_issues,
    }


def issues_blocking(issues: Sequence[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "blocker"]


def issues_retry_worthy(issues: Sequence[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity in ("blocker", "major")]


def format_issues_for_prompt(issues: Sequence[Issue]) -> str:
    if not issues:
        return "(no issues)"
    grouped: dict[str, list[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.source, []).append(issue)
    lines: list[str] = []
    for source, group in grouped.items():
        lines.append(f"## from {source}")
        for issue in group:
            lines.append(f"- [{issue.severity}] {issue.id} at {issue.location}")
            lines.append(f"    problem: {issue.problem}")
            lines.append(f"    repair: {issue.repair_instruction}")
    return "\n".join(lines)


# --- ALL_CHEAP_CHECKS (lazy to avoid circular imports) ---

_ALL_CHEAP_CHECKS: tuple[Check, ...] | None = None


def _build_all_cheap_checks() -> tuple[Check, ...]:
    from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
        SchemaCheck,
        PolicyAnchorCheck,
        IntentClarificationFormatCheck,
        RuntimeBoundaryLeakCheck,
        GuardrailHintInDataCheck,
        ViolatedActionsConcretenessCheck,
        CompletionCriteriaStepwiseCheck,
        CompletionViolationDecouplingCheck,
        WorkspaceFilesForTrajectorySemanticsCheck,
        GiteaSpecCheck,
        WebappDataShapeCheck,
        OracleUserPlaceholderCheck,
        InstructionVoiceCheck,
        InstructionMatchesSeedCheck,
    )
    return (
        SchemaCheck(),
        PolicyAnchorCheck(),
        IntentClarificationFormatCheck(),
        RuntimeBoundaryLeakCheck(),
        GuardrailHintInDataCheck(),
        ViolatedActionsConcretenessCheck(),
        CompletionCriteriaStepwiseCheck(),
        CompletionViolationDecouplingCheck(),
        WorkspaceFilesForTrajectorySemanticsCheck(),
        GiteaSpecCheck(),
        WebappDataShapeCheck(),
        OracleUserPlaceholderCheck(),
        InstructionVoiceCheck(),
        InstructionMatchesSeedCheck(),
    )


class _AllCheapChecksAccessor:
    def __iter__(self):
        return iter(self._get())

    def __len__(self):
        return len(self._get())

    def __getitem__(self, idx):
        return self._get()[idx]

    def _get(self):
        global _ALL_CHEAP_CHECKS
        if _ALL_CHEAP_CHECKS is None:
            _ALL_CHEAP_CHECKS = _build_all_cheap_checks()
        return _ALL_CHEAP_CHECKS


ALL_CHEAP_CHECKS = _AllCheapChecksAccessor()
