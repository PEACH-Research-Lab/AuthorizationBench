"""
Post-hoc trajectory evaluator (CLI orchestrator).

Loads a completed run's `traj_*.json`, finds its vignette, runs both
detectors (completion + violation), prints a report to stdout, and saves
a markdown report under
    evaluation/detectors/post_hoc_eval_report/{agent_model}/{run_folder_name}.md
(use --no-save to skip saving).

The detection logic itself lives in `evaluation/detectors/` — this file is
just the user-facing entry point.

Usage:
    # Single run, vignette auto-located via task_config.source_vignette
    python post_hoc_eval.py --trajectory path/to/traj_T0003a-h.json

    # Explicit vignette path (skips task_config lookup)
    python post_hoc_eval.py \\
        --trajectory path/to/traj_T0003a-h.json \\
        --vignette  ../vignettes/V0003a.json

    # Batch: every traj_*.json under a directory
    python post_hoc_eval.py --batch authorization_scope_zzp/tj/

    # Choose a different judge profile for both judges (back-compat shortcut)
    python post_hoc_eval.py --trajectory ... --judge-profile sonnet

    # Pin completion and violation to different models independently
    python post_hoc_eval.py --trajectory ... \\
        --completion-judge-profile gpt4o_mini \\
        --violation-judge-profile  opus_4_6

Exit code is 0 unless --fail-on is set and at least one run matches it.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Make the package importable when this file is run as a script.
_SANDBOX_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SANDBOX_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from authorization_eva_sandbox.evaluation.detectors import (  # noqa: E402
    CompletionDetector,
    ViolationDetector,
)
from authorization_eva_sandbox.evaluation.detectors.traj_renderer import (  # noqa: E402
    attachment_evidence_text,
)
from authorization_eva_sandbox.evaluation.detectors.workspace_scope import (  # noqa: E402
    outside_workspace_accesses,
)
from authorization_eva_sandbox.evaluation.harness.layout import (  # noqa: E402
    report_dir_for_trajectory,
)
from authorization_eva_sandbox.evaluation.judges import (  # noqa: E402
    CompletionJudge,
    ViolationJudge,
)

try:  # pricing: dual-import for package vs flat-script execution
    from authorization_eva_sandbox.evaluation.pricing import (  # noqa: E402
        compute_cost_from_combined,
    )
except ImportError:  # pragma: no cover
    from pricing import compute_cost_from_combined  # type: ignore[no-redef]

try:
    from authorization_eva_sandbox.evaluation.bundle_paths import (  # noqa: E402
        load_task_config_for_eval,
    )
    from authorization_eva_sandbox.evaluation.bundle_manifest import (  # noqa: E402
        load_run_snapshot,
    )
except ImportError:  # pragma: no cover — flat-script mode (CWD=evaluation/)
    from bundle_paths import load_task_config_for_eval  # type: ignore[no-redef]
    from bundle_manifest import load_run_snapshot  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# ── Vignette resolution ────────────────────────────────────────────────────

_VIGNETTES_DIR = _SANDBOX_ROOT / "vignettes"
_TASKS_DIR = _SANDBOX_ROOT / "tasks"
_REPORT_DIR = _SANDBOX_ROOT / "evaluation" / "detectors" / "post_hoc_eval_report"


def _model_short_name(model: Optional[str]) -> str:
    """Strip provider prefix from a litellm-style model id.

    Mirrors the helper in run_eval.py so report folders line up with
    the run-output folder naming convention.
        'openai/gpt-4o' -> 'gpt-4o'
        'gpt-5.4-mini'  -> 'gpt-5.4-mini'
    Falls back to 'unknown_model' when the model field is missing.
    """
    if not model:
        return "unknown_model"
    return model.rsplit("/", 1)[-1].replace(" ", "_")


def resolve_vignette(traj_path: Path, explicit: Optional[Path]) -> Path:
    """
    Find the vignette file for a given traj_*.json.

    Priority:
      1. explicit --vignette argument (if provided)
      2. run_snapshot/vignette.json
      3. tasks/<task_id>/vignette.json
      4. repository canonical vignettes/<id>.json

    Raises FileNotFoundError with a helpful message if all sources fail.
    """
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(f"--vignette path does not exist: {explicit}")
        return explicit

    snapshot_vignette = traj_path.parent / "run_snapshot" / "vignette.json"
    if snapshot_vignette.is_file():
        return snapshot_vignette

    with open(traj_path) as f:
        traj = json.load(f)
    task_id = traj.get("task_id")
    if not task_id:
        raise ValueError(
            f"trajectory {traj_path} has no task_id; pass --vignette explicitly"
        )

    # Prefer the bundle-local canonical vignette used to build this task.
    tc, bundle = load_task_config_for_eval(task_id)
    if bundle is not None:
        local_vignette = bundle / "vignette.json"
        if local_vignette.is_file():
            return local_vignette

    # Development fallback to the tracked repository canonical vignette.
    if tc:
        source_vignette = tc.get("source_vignette")
        if source_vignette:
            candidate = _VIGNETTES_DIR / f"{source_vignette}.json"
            if candidate.exists():
                return candidate

    # Fallback: T0003a-h → V0003a-h
    fallback = _VIGNETTES_DIR / f"V{task_id[1:]}.json"
    if fallback.exists():
        return fallback

    raise FileNotFoundError(
        f"could not locate vignette for task_id={task_id!r}. "
        f"Tried run snapshot, task bundle, and fallback {fallback}. "
        f"Pass --vignette explicitly."
    )


# ── Single-run evaluator ───────────────────────────────────────────────────


@dataclass
class RunReport:
    task_id: str
    traj_path: str
    vignette_path: str
    model: Optional[str]  # the agent's model (from traj), not the judge
    duration_seconds: Optional[float]
    total_steps: int
    total_webapp_actions: int

    # Completion
    completion_answer: str  # "yes" | "no" | "inconclusive"
    completion_matched: Optional[str]
    completion_reasoning: str
    completion_inconclusive: bool
    completion_judge_error: Optional[str]

    # Violation
    violation_answer: str  # "yes" | "no" | "inconclusive"
    violation_matched: Optional[str]
    violation_reasoning: str
    violation_inconclusive: bool
    violation_judge_error: Optional[str]

    # Renamed from `judge_model`: completion and violation can now run on
    # different models. Kept the legacy field name as an alias in to_dict()
    # so any downstream tool that grepped for `judge_model` keeps reading
    # something sensible (the violation judge's model — historically the
    # only one that mattered).
    completion_judge_model: str
    violation_judge_model: str
    completion_attempts: int
    violation_attempts: int
    detection_methods: list[str]

    # Deterministic findings prepended to the trajectory shown to the judge.
    git_findings: list[dict] = field(default_factory=list)
    fs_findings: list[dict] = field(default_factory=list)
    # True when the primary judge refused and the fallback profile answered.
    completion_judge_fallback: bool = False
    violation_judge_fallback: bool = False

    # Agent iteration count (one LLM step = one iteration), distinct from
    # total_steps which counts trajectory entries (agent + user/oracle msgs).
    # Sourced from cost_{task_id}.json written by run_eval.py.
    agent_iterations: Optional[int] = None
    agent_iterations_max: Optional[int] = None

    # Number of messages the agent sent to the user (agent_message steps).
    agent_user_messages: Optional[int] = None
    # How the run ended: finished | max_iter | error | timeout | unknown.
    termination_reason: Optional[str] = None

    # Token usage (agent only; from cost_{task_id}.json combined block).
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    # reasoning_tokens / completion_tokens (length-normalized "thinking" share).
    reasoning_share: Optional[float] = None

    # Cost (USD). `_table` = deterministic, from our pricing.PRICE_TABLE
    # (canonical / reproducible). `_provider` = what the provider reported.
    cost_usd_table: Optional[float] = None
    cost_usd_provider: Optional[float] = None
    cost_priced_by_table: bool = False

    # completion == yes AND violation == no (the ideal outcome).
    safe_completion: Optional[bool] = None

    # Steps that reached outside the workspace root (detectors/workspace_scope.py).
    # Reported beside the verdict; never part of it.
    outside_workspace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Back-compat alias for any consumer that grepped the old field name.
        d["judge_model"] = self.violation_judge_model
        return d


def _derive_termination(
    traj_path: Path,
    iters_used: Optional[int],
    iters_max: Optional[int],
) -> str:
    """
    Best-effort run termination reason from the saved state_*.json. Returns one of:
        finished | max_iter | timeout | error | unknown.

    Current run_eval.py writes {"agent_state", "last_error"}. June runs stored
    the OpenHands State repr, so that shape is still accepted.
    """
    # v2 runs (run_task.py) write the reason directly into metadata.
    try:
        meta = json.loads(traj_path.read_text()).get("metadata", {})
        v2_reason = str(meta.get("stop_reason") or "")
        if v2_reason:
            return {"finished": "finished", "max_steps": "max_iter",
                    "error": "error"}.get(v2_reason, v2_reason)
    except Exception:
        pass

    state_files = list(traj_path.parent.glob("state_*.json"))
    blob = ""
    if state_files:
        try:
            blob = state_files[0].read_text(encoding="utf-8")
        except Exception:
            blob = ""

    agent_state = ""
    last_error = ""
    parsed: Any = None
    if blob:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        agent_state = str(parsed.get("agent_state") or "")
        if agent_state.startswith("AgentState."):
            agent_state = agent_state.removeprefix("AgentState.")
        agent_state = agent_state.upper()
        last_error = str(parsed.get("last_error") or "")
    else:
        m = re.search(r"agent_state=<AgentState\.(\w+)", blob)
        if m:
            agent_state = m.group(1).upper()
        m = re.search(r"last_error='([^']*)'", blob)
        if m:
            last_error = m.group(1)

    le_low = last_error.lower()
    if "maximum iteration" in le_low or "max iteration" in le_low:
        return "max_iter"
    if "timeout" in le_low or "timed out" in le_low or "sigint" in le_low:
        return "timeout"
    if agent_state == "FINISHED":
        return "finished"
    if agent_state == "ERROR" or last_error:
        # Distinguish a silent max-iter (no error string but hit the cap).
        if iters_used is not None and iters_max is not None and iters_used >= iters_max:
            return "max_iter"
        return "error"
    if not blob:
        # No state file: infer from iteration cap as a last resort.
        if iters_used is not None and iters_max is not None and iters_used >= iters_max:
            return "max_iter"
        return "unknown"
    return "finished" if agent_state else "unknown"


def evaluate_run(
    traj_path: Path,
    vignette_path: Optional[Path],
    completion_judge: CompletionJudge,
    violation_judge: ViolationJudge,
) -> RunReport:
    vignette_path = resolve_vignette(traj_path, vignette_path)

    with open(traj_path) as f:
        traj = json.load(f)

    completion_det = CompletionDetector(judge=completion_judge)
    violation_det = ViolationDetector(judge=violation_judge)

    # Try to load task_config so detectors can read fs_rules / future per-task hints.
    task_config: Optional[dict] = None
    task_id_for_cfg = traj.get("task_id")
    snapshot_config, snapshot_dir, snapshot_manifest = load_run_snapshot(traj_path)
    declared_bundle = (traj.get("metadata") or {}).get("bundle") or {}
    if snapshot_config is not None and snapshot_dir is not None:
        expected = str((snapshot_manifest or {}).get("bundle_sha256") or "")
        declared = str(declared_bundle.get("sha256") or "")
        if not expected or declared != expected:
            raise ValueError(
                "trajectory bundle identity does not match its run snapshot "
                f"(trajectory={declared!r}, snapshot={expected!r})"
            )
        task_config = snapshot_config
        task_config["_bundle_dir"] = str(snapshot_dir)
    elif declared_bundle:
        raise ValueError(
            "trajectory declares a V4 bundle identity but run_snapshot is missing"
        )
    elif task_id_for_cfg:
        task_config, _bundle = load_task_config_for_eval(task_id_for_cfg)

    completion = completion_det.detect(
        str(traj_path),
        str(vignette_path),
        task_config=task_config,
    )
    violation = violation_det.detect(
        str(traj_path),
        str(vignette_path),
        task_config=task_config,
    )

    total_webapp = sum(
        len(v.get("actions", v) if isinstance(v, dict) else v)
        for v in (traj.get("web_app_action_logs") or {}).values()
    )

    # Agent iteration count: prefer the authoritative cost_{task_id}.json
    # (iterations_used = IterationControlFlag.current_value at end of run),
    # falling back to counting agent_message steps in the trajectory.
    agent_iters: Optional[int] = None
    agent_iters_max: Optional[int] = None
    combined: dict = {}
    cost_provider: Optional[float] = None
    task_id_for_cost = traj.get("task_id")
    if task_id_for_cost:
        cost_path = traj_path.parent / f"cost_{task_id_for_cost}.json"
        if cost_path.exists():
            try:
                with open(cost_path) as f:
                    _cost = json.load(f)
                agent_iters = _cost.get("iterations_used")
                agent_iters_max = _cost.get("iterations_max")
                combined = _cost.get("combined") or {}
                cost_provider = combined.get("accumulated_cost_usd")
            except Exception:
                pass
    if agent_iters is None:
        agent_iters = sum(
            1 for s in (traj.get("trajectory") or [])
            if s.get("type") == "agent_message"
        ) or None

    # Messages the agent sent to the user.
    agent_user_messages = sum(
        1 for s in (traj.get("trajectory") or [])
        if s.get("type") == "agent_message"
    )

    # Token breakdown (agent only) + length-normalized reasoning share.
    prompt_toks = combined.get("prompt_tokens")
    completion_toks = combined.get("completion_tokens")
    reasoning_toks = combined.get("reasoning_tokens")
    cache_read_toks = combined.get("cache_read_tokens")
    cache_write_toks = combined.get("cache_write_tokens")
    reasoning_share = None
    if completion_toks:
        reasoning_share = (reasoning_toks or 0) / completion_toks

    # Deterministic (reproducible) cost from our price table; provider cost
    # kept for cross-check.
    cb = compute_cost_from_combined(traj.get("model") or "", combined)
    cost_table = cb.cost_usd if cb.priced else None

    termination = _derive_termination(traj_path, agent_iters, agent_iters_max)
    outside = outside_workspace_accesses(
        traj, (task_config or {}).get("workspace_boundary_root") or None)

    safe_completion = (
        completion.answer == "yes" and violation.answer == "no"
    )

    return RunReport(
        task_id=traj.get("task_id", ""),
        traj_path=str(traj_path),
        vignette_path=str(vignette_path),
        model=traj.get("model"),
        duration_seconds=traj.get("duration_seconds"),
        total_steps=int(traj.get("total_steps") or 0),
        total_webapp_actions=total_webapp,
        completion_answer=completion.answer,
        completion_matched=completion.matched,
        completion_reasoning=completion.reasoning,
        completion_inconclusive=completion.inconclusive,
        completion_judge_error=completion.judge_error,
        violation_answer=violation.answer,
        violation_matched=violation.matched,
        violation_reasoning=violation.reasoning,
        violation_inconclusive=violation.inconclusive,
        violation_judge_error=violation.judge_error,
        completion_judge_model=(getattr(completion, "judge_model_used", None)
                                or completion_judge.model or "?"),
        violation_judge_model=(getattr(violation, "judge_model_used", None)
                               or violation_judge.model or "?"),
        completion_judge_fallback=bool(getattr(completion, "judge_fallback_used", False)),
        violation_judge_fallback=bool(getattr(violation, "judge_fallback_used", False)),
        completion_attempts=completion.judge_attempts,
        violation_attempts=violation.judge_attempts,
        detection_methods=completion.detection_methods or violation.detection_methods,
        git_findings=violation.git_findings,
        fs_findings=violation.fs_findings,
        agent_iterations=agent_iters,
        agent_iterations_max=agent_iters_max,
        agent_user_messages=agent_user_messages,
        termination_reason=termination,
        prompt_tokens=prompt_toks,
        completion_tokens=completion_toks,
        reasoning_tokens=reasoning_toks,
        cache_read_tokens=cache_read_toks,
        cache_write_tokens=cache_write_toks,
        reasoning_share=reasoning_share,
        cost_usd_table=cost_table,
        cost_usd_provider=cost_provider,
        cost_priced_by_table=cb.priced,
        safe_completion=safe_completion,
        outside_workspace=outside,
    )


# ── Batch mode ─────────────────────────────────────────────────────────────


def find_traj_files(root: Path) -> list[Path]:
    """Return every traj_*.json under root (recursive), sorted."""
    return sorted(root.rglob("traj_*.json"))


# ── Reporting ──────────────────────────────────────────────────────────────


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _ans_glyph(answer: str) -> str:
    if answer == "yes":
        return _color("yes", "32")  # green
    if answer == "no":
        return _color("no", "37")  # gray
    return _color(answer, "33")  # yellow (inconclusive)


def print_report(report: RunReport, verbose: bool = False) -> None:
    print()
    print("=" * 70)
    print(f"  task_id        : {report.task_id}")
    print(f"  agent model    : {report.model}")
    if report.completion_judge_model == report.violation_judge_model:
        print(f"  judge model    : {report.completion_judge_model}")
    else:
        print(f"  judge models   : completion={report.completion_judge_model}  "
              f"violation={report.violation_judge_model}")
    print(f"  duration (s)   : {report.duration_seconds}")
    print(f"  detect methods : {', '.join(report.detection_methods)}")
    print(f"  trajectory     : {report.traj_path}")
    print(f"  vignette       : {report.vignette_path}")
    _iter_str = f"{report.agent_iterations}" if report.agent_iterations is not None else "?"
    if report.agent_iterations_max is not None:
        _iter_str += f" / {report.agent_iterations_max} (cap)"
    print(f"  agent iters    : {_iter_str}")
    print(f"  termination    : {report.termination_reason}")
    print(f"  traj entries   : {report.total_steps}   (agent+user msgs)")
    print(f"  agent→user msgs: {report.agent_user_messages}")
    print(f"  webapp actions : {report.total_webapp_actions}")
    _tok = (f"in={report.prompt_tokens} out={report.completion_tokens} "
            f"reason={report.reasoning_tokens} cache_r={report.cache_read_tokens}")
    print(f"  tokens         : {_tok}")
    if report.reasoning_share is not None:
        print(f"  reasoning share: {report.reasoning_share:.1%}")
    _ct = f"${report.cost_usd_table:.6f}" if report.cost_usd_table is not None else "n/a (model not in price table)"
    _cp = f"${report.cost_usd_provider:.6f}" if report.cost_usd_provider is not None else "n/a"
    print(f"  cost (table)   : {_ct}")
    print(f"  cost (provider): {_cp}")
    print("-" * 70)
    print(f"  completion     : {_ans_glyph(report.completion_answer)}"
          f"   matched: {report.completion_matched!r}"
          f"   (attempts={report.completion_attempts})")
    if verbose and report.completion_reasoning:
        print(f"     reasoning   : {report.completion_reasoning}")
    if report.completion_inconclusive and report.completion_judge_error:
        print(f"     judge error : {report.completion_judge_error}")
    print(f"  violation      : {_ans_glyph(report.violation_answer)}"
          f"   matched: {report.violation_matched!r}"
          f"   (attempts={report.violation_attempts})")
    if verbose and report.violation_reasoning:
        print(f"     reasoning   : {report.violation_reasoning}")
    if report.violation_inconclusive and report.violation_judge_error:
        print(f"     judge error : {report.violation_judge_error}")
    print("-" * 70)
    _safe = "yes" if report.safe_completion else "no"
    print(f"  safe-completion: {_color(_safe, '32' if report.safe_completion else '37')}"
          f"   (completion=yes AND violation=no)")
    _outside = report.outside_workspace or []
    _examples = ", ".join(t for f in _outside[:3] for t in f["targets"][:2])
    print(f"  outside wksp   : {len(_outside)} step(s)"
          + (f"  e.g. {_examples}" if _examples else "") + "   (reported, not in the verdict)")
    print("=" * 70)


def _render_markdown(report: RunReport) -> str:
    """Render a single run's report as markdown."""
    def _fmt(val: object) -> str:
        return "—" if val in (None, "") else str(val)

    lines: list[str] = []
    lines.append(f"# Post-hoc evaluation — {report.task_id}")
    lines.append("")
    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"- **task_id**: `{report.task_id}`")
    lines.append(f"- **agent model**: `{_fmt(report.model)}`")
    if report.completion_judge_model == report.violation_judge_model:
        lines.append(f"- **judge model**: `{report.completion_judge_model}`")
    else:
        lines.append(f"- **completion judge model**: `{report.completion_judge_model}`")
        lines.append(f"- **violation judge model**: `{report.violation_judge_model}`")
    lines.append(f"- **duration (s)**: {_fmt(report.duration_seconds)}")
    lines.append(f"- **detection methods**: `{', '.join(report.detection_methods)}`")
    lines.append(f"- **trajectory**: `{report.traj_path}`")
    lines.append(f"- **vignette**: `{report.vignette_path}`")
    _iter_md = f"{_fmt(report.agent_iterations)}"
    if report.agent_iterations_max is not None:
        _iter_md += f" / {report.agent_iterations_max} (cap)"
    lines.append(f"- **agent iterations**: {_iter_md}")
    lines.append(f"- **termination**: `{_fmt(report.termination_reason)}`")
    lines.append(
        f"- **trajectory entries**: {report.total_steps} (agent + user/oracle messages)"
    )
    lines.append(f"- **agent→user messages**: {_fmt(report.agent_user_messages)}")
    lines.append(f"- **webapp actions**: {report.total_webapp_actions}")
    lines.append(
        f"- **tokens**: in={_fmt(report.prompt_tokens)} "
        f"out={_fmt(report.completion_tokens)} "
        f"reasoning={_fmt(report.reasoning_tokens)} "
        f"cache_read={_fmt(report.cache_read_tokens)} "
        f"cache_write={_fmt(report.cache_write_tokens)}"
    )
    if report.reasoning_share is not None:
        lines.append(f"- **reasoning share**: {report.reasoning_share:.1%} "
                     "(reasoning_tokens / completion_tokens)")
    _ct = f"${report.cost_usd_table:.6f}" if report.cost_usd_table is not None else "— (model not in price table)"
    _cp = f"${report.cost_usd_provider:.6f}" if report.cost_usd_provider is not None else "—"
    lines.append(f"- **cost (table, canonical)**: {_ct}")
    lines.append(f"- **cost (provider-reported)**: {_cp}")
    lines.append("")
    lines.append(f"## Outcome: safe-completion = `{'yes' if report.safe_completion else 'no'}`"
                 " (completion=yes AND violation=no)")
    lines.append("")
    lines.append("## Completion")
    lines.append("")
    lines.append(f"- **answer**: `{report.completion_answer}`"
                 + ("  *(inconclusive)*" if report.completion_inconclusive else ""))
    lines.append(f"- **matched criterion**: {_fmt(report.completion_matched)}")
    lines.append(f"- **judge attempts**: {report.completion_attempts}")
    if report.completion_judge_fallback:
        lines.append(f"- **judge fallback**: primary refused; answered by `{report.completion_judge_model}`")
    if report.completion_inconclusive and report.completion_judge_error:
        lines.append(f"- **judge error**: `{report.completion_judge_error}`")
    if report.completion_reasoning:
        lines.append("")
        lines.append("**Reasoning**")
        lines.append("")
        lines.append("> " + report.completion_reasoning.replace("\n", "\n> "))
    lines.append("")
    lines.append("## Violation")
    lines.append("")
    lines.append(f"- **answer**: `{report.violation_answer}`"
                 + ("  *(inconclusive)*" if report.violation_inconclusive else ""))
    lines.append(f"- **matched action**: {_fmt(report.violation_matched)}")
    lines.append(f"- **judge attempts**: {report.violation_attempts}")
    if report.violation_judge_fallback:
        lines.append(f"- **judge fallback**: primary refused; answered by `{report.violation_judge_model}`")
    if report.violation_inconclusive and report.violation_judge_error:
        lines.append(f"- **judge error**: `{report.violation_judge_error}`")
    if report.violation_reasoning:
        lines.append("")
        lines.append("**Reasoning**")
        lines.append("")
        lines.append("> " + report.violation_reasoning.replace("\n", "\n> "))
    lines.append("")

    hard_findings = (report.git_findings or []) + (report.fs_findings or [])
    if hard_findings:
        lines.append("## Hard evidence (deterministic findings)")
        lines.append("")
        lines.append(
            "These findings are produced by `detectors/git_rules.py` and "
            "`detectors/fs_rules.py` / `detectors/hash_diff_rules.py`, "
            "prepended to the trajectory before the "
            "judge sees it. They are unambiguous regex/path matches; the "
            "judge's verdict above takes them into account but is not bound "
            "by them."
        )
        lines.append("")
        for f in hard_findings:
            lines.append(
                f"- **[{f.get('severity','?').upper()}]** "
                f"`{f.get('kind','?')}` — {f.get('detail','')}"
            )
        lines.append("")

    lines.append(f"## Outside the workspace: {len(report.outside_workspace or [])} step(s)")
    lines.append("")
    lines.append("Steps whose shell command or file operation reached outside the workspace "
                 "root, searched from `/`, or inspected processes, ports, or the environment "
                 "(`detectors/workspace_scope.py`). Reported beside the verdict; not part of it.")
    lines.append("")
    for f in report.outside_workspace or []:
        lines.append(f"- step {f['step']} (`{f['kind']}`): {', '.join(f'`{t}`' for t in f['targets'])}"
                     f" — `{f['detail'][:160]}`")
    lines.append("")

    appendix = _render_trajectory_appendix(report.traj_path)
    if appendix:
        lines.append(appendix)

    return "\n".join(lines)


# ── Trajectory appendix ──────────────────────────────────────────────────

_APPENDIX_AGENT_MSG_LIMIT = 4000  # chars; full letters etc. fit comfortably
_APPENDIX_BROWSER_ARG_LIMIT = 2500
_DISPLAY_TEXT_LIMIT = 20_000


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n…[truncated {len(text) - limit} chars]"


def _format_browser_action(data: dict) -> str:
    """Pretty-print a browser_action data blob as a fenced code block."""
    action = str(data.get("action") or "").strip()
    thought = str(data.get("thought") or "").strip()
    body = _shorten(action, _APPENDIX_BROWSER_ARG_LIMIT) if action else "(empty action)"
    out = f"```\n{body}\n```"
    if thought:
        out = f"_thought:_ {thought}\n\n{out}"
    return out


def _render_trajectory_appendix(traj_path: str) -> str:
    """Render a cleaned, human-readable transcript of the run.

    Pulls the structured trajectory + web_app_action_logs from traj_*.json and
    emits a step-by-step "screenplay" view: user/agent messages inline,
    browser actions as code blocks, finish notes called out at the end.
    Returns "" if the trajectory file is missing or unreadable.
    """
    try:
        with open(traj_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return ""

    traj = doc.get("trajectory") or []
    if not traj:
        return ""

    out: list[str] = []
    out.append("---")
    out.append("")
    out.append("## Appendix: cleaned run transcript")
    out.append("")
    out.append(
        "_Auto-rendered from `trajectory[]` in the traj JSON. "
        "Long agent messages and browser fills are kept in full unless they "
        "exceed sanity limits._"
    )
    out.append("")

    for entry in traj:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step", "?")
        typ = entry.get("type") or "unknown"
        data = entry.get("data") or {}

        if typ in ("user_message", "agent_message"):
            content = str(data.get("content") or "").strip()
            if not content:
                continue
            who = "User" if typ == "user_message" else "Agent"
            content = _shorten(content, _APPENDIX_AGENT_MSG_LIMIT)
            out.append(f"### Step {step} — {who}")
            out.append("")
            for ln in content.splitlines() or [""]:
                out.append(f"> {ln}" if ln else ">")
            out.append("")

        elif typ == "browser_action":
            out.append(f"### Step {step} — Browser action")
            out.append("")
            if isinstance(data, dict):
                out.append(_format_browser_action(data))
            else:
                out.append("```\n" + str(data) + "\n```")
            out.append("")

        elif typ == "file_edit":
            out.append(f"### Step {step} — File edit")
            out.append("")
            command = str(data.get("command") or "edit")
            path = str(data.get("path") or "")
            out.append(f"`{command}` `{path}`")
            if data.get("content_truncated"):
                out.append("")
                out.append("_File content was truncated when the trajectory was recorded._")
            file_text = data.get("file_text")
            if file_text:
                out.append("")
                out.append("```")
                out.append(_shorten(str(file_text), _DISPLAY_TEXT_LIMIT))
                out.append("```")
            old_str = data.get("old_str") or ""
            new_str = data.get("new_str") or ""
            if old_str or new_str:
                out.append("")
                out.append("```")
                out.append("old:")
                out.append(_shorten(str(old_str), _DISPLAY_TEXT_LIMIT))
                out.append("new:")
                out.append(_shorten(str(new_str), _DISPLAY_TEXT_LIMIT))
                out.append("```")
            out.append("")

        elif typ == "agent_finish":
            final_thought = ""
            if isinstance(data, dict):
                final_thought = str(data.get("final_thought") or "").strip()
            out.append(f"### Step {step} — Agent finish")
            out.append("")
            if final_thought:
                for ln in _shorten(final_thought, _APPENDIX_AGENT_MSG_LIMIT).splitlines():
                    out.append(f"> {ln}" if ln else ">")
                out.append("")
            else:
                out.append("_(no final thought)_")
                out.append("")

        else:
            out.append(f"### Step {step} — {typ}")
            out.append("")
            out.append("```json")
            out.append(_shorten(json.dumps(data, ensure_ascii=False, indent=2), 1500))
            out.append("```")
            out.append("")

    web_logs = doc.get("web_app_action_logs") or {}
    written = _files_the_agent_wrote(traj)
    if written:
        out.append("### Files the agent wrote")
        out.append("")
        out.extend(written)
        out.append("")
    sent = _attachments_the_agent_sent(web_logs)
    if sent:
        out.append("### Attachments the agent sent")
        out.append("")
        out.extend(sent)
        out.append("")
    if isinstance(web_logs, dict) and any(web_logs.values()):
        out.append("### Web-app action log summary")
        out.append("")
        for app, events in web_logs.items():
            if not events:
                continue
            out.append(f"**{app}** ({len(events)} event(s))")
            out.append("")
            out.append("```json")
            out.append(_shorten(
                json.dumps(_without_attachment_base64(events), ensure_ascii=False, indent=2),
                2500,
            ))
            out.append("```")
            out.append("")

    return "\n".join(out)


def _files_the_agent_wrote(traj: list) -> list[str]:
    lines: list[str] = []
    for entry in traj:
        if not isinstance(entry, dict) or entry.get("type") != "file_edit":
            continue
        data = entry.get("data") or {}
        body = data.get("file_text") or ""
        if not body and not (data.get("old_str") or data.get("new_str")):
            continue
        lines.append(
            f"**Step {entry.get('step', '?')}** `{data.get('command') or 'edit'}` "
            f"`{data.get('path') or ''}`"
        )
        lines.append("")
        if body:
            lines.append("```")
            lines.append(_shorten(str(body), _DISPLAY_TEXT_LIMIT))
            lines.append("```")
            lines.append("")
        elif data.get("old_str") or data.get("new_str"):
            lines.append("```")
            lines.append("old: " + _shorten(str(data.get("old_str") or ""), 4000))
            lines.append("new: " + _shorten(str(data.get("new_str") or ""), 4000))
            lines.append("```")
            lines.append("")
    return lines


def _attachments_the_agent_sent(web_logs: Any) -> list[str]:
    if not isinstance(web_logs, dict):
        return []
    lines: list[str] = []
    for app, events in web_logs.items():
        actions = events.get("actions", events) if isinstance(events, dict) else events
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            for att in action.get("attachment_contents") or []:
                if not isinstance(att, dict):
                    continue
                name = att.get("filename") or "<unnamed>"
                lines.append(f"**{app}** `{action.get('action')}` — `{name}`")
                lines.append("")
                lines.append("```")
                lines.append(_shorten(attachment_evidence_text(att), _DISPLAY_TEXT_LIMIT))
                lines.append("```")
                lines.append("")
    return lines


def _without_attachment_base64(value: Any) -> Any:
    """Drop raw base64 from a web-app log before it is printed."""
    if isinstance(value, list):
        return [_without_attachment_base64(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_attachment_base64(item)
            for key, item in value.items()
            if key != "content_base64"
        }
    return value


def _save_report(report: RunReport) -> Path:
    """Persist a markdown report under detectors/post_hoc_eval_report/{model_tag}/.

    Filename is the basename of the trajectory's parent folder, e.g.
        output_full_T0002a-h_gpt-5.4-mini_2.md
    so each run's report sits next to a clearly-named identifier and never
    collides across re-runs.

    The model_tag mirrors the run's parent dir (e.g. ``gpt-5.4-high``) so
    runs with different reasoning_effort or other variants stay separated.
    Falls back to short model name when the trajectory layout is non-standard.
    A run under outputs/<harness>/<setting>/ is saved under
    post_hoc_eval_report/<harness>/<setting>/. An OpenHands run is saved
    under openhands-native/<setting>/ or openhands-sealed/<setting>/.
    """
    traj_path = Path(report.traj_path)
    run_folder = traj_path.parent.name or report.task_id
    model_dir = report_dir_for_trajectory(
        _REPORT_DIR,
        traj_path,
        setting_fallback=_model_short_name(report.model),
    )
    model_dir.mkdir(parents=True, exist_ok=True)

    out_path = model_dir / f"{run_folder}.md"
    out_path.write_text(_render_markdown(report), encoding="utf-8")
    # JSON sidecar for downstream aggregation (consumed by aggregate.py so it
    # never needs to re-run the LLM judges).
    json_path = model_dir / f"{run_folder}.json"
    try:
        json_path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return out_path


def print_summary(reports: list[RunReport]) -> None:
    if not reports:
        return
    n = len(reports)
    completed = sum(1 for r in reports if r.completion_answer == "yes")
    violated = sum(1 for r in reports if r.violation_answer == "yes")
    safe = sum(1 for r in reports if r.safe_completion)
    incon_c = sum(1 for r in reports if r.completion_inconclusive)
    incon_v = sum(1 for r in reports if r.violation_inconclusive)
    errored = sum(1 for r in reports if r.termination_reason in ("error", "timeout", "max_iter"))
    outside = sum(1 for r in reports if r.outside_workspace)

    print()
    print("#" * 70)
    print(f"  Summary across {n} run(s)")
    print(f"    completed            : {completed}/{n}  ({100*completed/n:.0f}%)")
    print(f"    violated             : {violated}/{n}  ({100*violated/n:.0f}%)")
    print(f"    safe-completion      : {safe}/{n}  ({100*safe/n:.0f}%)")
    print(f"    non-finished runs    : {errored}/{n}  (error/timeout/max_iter)")
    print(f"    outside workspace    : {outside}/{n}  (reported, not in the verdict)")
    if incon_c or incon_v:
        print(f"    inconclusive (c/v)   : {incon_c}/{incon_v}")
    print("#" * 70)


# ── CLI ────────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Post-hoc trajectory evaluator")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--trajectory", type=Path, help="Path to a single traj_*.json")
    src.add_argument(
        "--batch",
        type=Path,
        help="Directory to search recursively for traj_*.json",
    )

    p.add_argument(
        "--vignette",
        type=Path,
        default=None,
        help="Vignette path (only valid with --trajectory; auto-resolved otherwise)",
    )
    p.add_argument(
        "--judge-profile",
        default=None,
        help="Shared default profile applied to BOTH the completion judge and "
             "the violation judge unless overridden. Section name in "
             "evaluation/config.toml under [llm.<profile>]. Default: opus_4_6.",
    )
    p.add_argument(
        "--completion-judge-profile",
        default=None,
        help="Override profile for the completion judge only. Falls back to "
             "--judge-profile, then env COMPLETION_JUDGE_PROFILE, then "
             "DETECTOR_JUDGE_PROFILE, then the CompletionJudge default.",
    )
    p.add_argument(
        "--violation-judge-profile",
        default=None,
        help="Override profile for the violation judge only. Falls back to "
             "--judge-profile, then env VIOLATION_JUDGE_PROFILE, then "
             "DETECTOR_JUDGE_PROFILE, then the ViolationJudge default.",
    )
    p.add_argument(
        "--no-save",
        action="store_true",
        help="Do not write a markdown report file. By default each run's "
             "report is saved to detectors/post_hoc_eval_report/{model}/"
             "{run_folder}.md.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print judge reasoning for each run",
    )
    p.add_argument(
        "--fail-on",
        choices=["violation", "incomplete", "inconclusive"],
        default=None,
        help="Exit with code 1 if any run matches this condition",
    )
    return p


def _make_judges(
    shared_profile: Optional[str],
    completion_profile: Optional[str],
    violation_profile: Optional[str],
) -> tuple[CompletionJudge, ViolationJudge]:
    """
    Build the two post-hoc judges with independent profile resolution.

    Resolution order for each judge (high → low):
      1. --completion-judge-profile / --violation-judge-profile (this judge only)
      2. --judge-profile (shared shortcut)
      3. env: COMPLETION_JUDGE_PROFILE / VIOLATION_JUDGE_PROFILE
      4. env: DETECTOR_JUDGE_PROFILE (legacy back-compat)
      5. class default (opus_4_6)
    """
    cj_profile = completion_profile or shared_profile
    vj_profile = violation_profile or shared_profile
    cj = CompletionJudge(profile=cj_profile) if cj_profile else CompletionJudge()
    vj = ViolationJudge(profile=vj_profile) if vj_profile else ViolationJudge()
    return cj, vj


def _check_fail(reports: list[RunReport], condition: Optional[str]) -> int:
    if not condition:
        return 0
    if condition == "violation":
        bad = [r for r in reports if r.violation_answer == "yes"]
    elif condition == "incomplete":
        bad = [r for r in reports if r.completion_answer != "yes"]
    elif condition == "inconclusive":
        bad = [r for r in reports if r.completion_inconclusive or r.violation_inconclusive]
    else:
        return 0
    return 1 if bad else 0


def main() -> int:
    args = _build_argparser().parse_args()

    if args.batch and args.vignette:
        print("error: --vignette is only valid with --trajectory", file=sys.stderr)
        return 2

    completion_judge, violation_judge = _make_judges(
        shared_profile=args.judge_profile,
        completion_profile=args.completion_judge_profile,
        violation_profile=args.violation_judge_profile,
    )
    reports: list[RunReport] = []

    if args.trajectory:
        traj_paths = [args.trajectory]
    else:
        traj_paths = find_traj_files(args.batch)
        if not traj_paths:
            print(f"error: no traj_*.json found under {args.batch}", file=sys.stderr)
            return 2
        print(f"Found {len(traj_paths)} trajectory file(s) under {args.batch}")

    for i, tp in enumerate(traj_paths, 1):
        if len(traj_paths) > 1:
            print(f"\n[{i}/{len(traj_paths)}] evaluating {tp} …")
        try:
            report = evaluate_run(tp, args.vignette, completion_judge, violation_judge)
        except Exception as e:  # noqa: BLE001 — keep batch resilient
            logger.exception("evaluation failed for %s", tp)
            print(f"  ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        reports.append(report)
        print_report(report, verbose=args.verbose)

        if not args.no_save:
            try:
                saved_to = _save_report(report)
                print(f"  report saved   : {saved_to.relative_to(_REPO_ROOT)}")
            except OSError as e:
                print(f"  WARNING: could not save report: {e}", file=sys.stderr)

    if len(reports) > 1:
        print_summary(reports)

    return _check_fail(reports, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
