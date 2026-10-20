"""Plan benchmark runs for OpenHands and the OpenAI Agents SDK.

The planner prints one shell command per missing run, grouped into rounds,
and writes the same plan as JSON. Each run stays in its own terminal; this
tool only decides the commands and reports status.

    python -m authorization_eva_sandbox.tools.bench_plan plan \\
        --tasks T0003-g-b-h,T0004-b-c-h --profiles all \\
        --harnesses openhands,openai_agents --repeat 1 \\
        --since 2026-09-24T03:27 --round-size 8 --timeout 1800
    python -m authorization_eva_sandbox.tools.bench_plan status \\
        --tasks T0003-g-b-h --profiles all --harnesses openhands,openai_agents
    python -m authorization_eva_sandbox.tools.bench_plan plan --prompt native \\
        --tasks T0003-g-b-h --profiles all --harnesses openai_agents

A run counts toward ``--repeat`` when it has a post-hoc report, is absent from
outputs/benchmark_run_superseded.json, ran under the ``--prompt`` condition
with the current prompt fingerprint, matches the task's current agent-facing
version, started at or after ``--since``, and ended as an agent outcome:
finished, max_steps, max_iter, timeout, or output_cap. Anything else is a
failure and is planned again.

``--prompt`` is ``sealed`` (default) or ``native``. A run's condition and
prompt fingerprint come from ``metadata.prompt``; a run without it is sealed.
The native fingerprint covers the harness's native prompt hash, the composer
version and the environment notes (``prompt_composition.prompt_fingerprint``),
so sealed and native runs never count for each other, and a native run goes
stale when the native prompt or our added text changes.

The agent-facing version hashes only inputs a harness can show the agent or
the simulated user. A run whose agent-facing version matches, but whose
judge-facing criteria differ from the task, stays valid and is listed under
``rejudge``. Runs record the full bundle hash only. When a run folder has a
usable ``run_snapshot/``, the agent-facing version is recomputed from that
snapshot. A run with no usable snapshot falls back to the full bundle hash
(first 8 hex characters of ``build_bundle_manifest``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import statistics
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from authorization_eva_sandbox.evaluation.bundle_manifest import (
    SNAPSHOT_DIR,
    build_bundle_manifest,
)
from authorization_eva_sandbox.evaluation import run_records
from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    DEFAULT_PROMPT_CONDITION,
    PROMPT_CONDITIONS,
    SEALED,
    check_condition,
    prompt_fingerprint,
    recorded_condition,
)

SANDBOX_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SANDBOX_ROOT.parent

# Profile name (config.toml [llm.*]) → output setting directory.
PROFILES: tuple[tuple[str, str], ...] = (
    ("gpt5_6_sol_low", "gpt-5.6-sol-low"),
    ("gpt5_6_sol_high", "gpt-5.6-sol-high"),
    ("sonnet5_low", "claude-sonnet-5-low"),
    ("sonnet5_high", "claude-sonnet-5-high"),
    ("gemini38_flash_low", "gemini-3.8-flash-low"),
    ("gemini38_flash_high", "gemini-3.8-flash-high"),
    ("glm53_low", "glm-5.3-low"),
    ("glm53_high", "glm-5.3-high"),
)
PROFILE_TO_SETTING = dict(PROFILES)
SETTING_TO_PROFILE = {setting: profile for profile, setting in PROFILES}

HARNESSES = ("openhands", "openai_agents", "openai_sandbox")
INTERPRETERS = {
    "openhands": ".venv-openhands/bin/python",
    "openai_agents": ".venv-openai-agents/bin/python",
    "openai_sandbox": ".venv-openai-agents/bin/python",
}
# Agent outcomes. Infrastructure failures (error, unknown, a missing report)
# are planned again.
AGENT_OUTCOMES = {"finished", "max_steps", "max_iter", "timeout", "output_cap"}

# Direct providers. GLM is its own bucket so it does not pile up with the
# other OpenRouter models (Anthropic, Google).
PROVIDERS = {
    "gpt5_6_sol_low": "openai",
    "gpt5_6_sol_high": "openai",
    "sonnet5_low": "anthropic",
    "sonnet5_high": "anthropic",
    "gemini38_flash_low": "google",
    "gemini38_flash_high": "google",
    "glm53_low": "glm",
    "glm53_high": "glm",
}
OPENROUTER = {"anthropic", "google", "glm"}

PROFILE_SHORT = {
    "gpt5_6_sol_low": "sol-L",
    "gpt5_6_sol_high": "sol-H",
    "sonnet5_low": "son-L",
    "sonnet5_high": "son-H",
    "gemini38_flash_low": "gem-L",
    "gemini38_flash_high": "gem-H",
    "glm53_low": "glm-L",
    "glm53_high": "glm-H",
}
HARNESS_SHORT = {"openhands": "oh", "openai_agents": "oa", "openai_sandbox": "os"}

_TASK_ID = re.compile(r"^T\d{4}-[a-z]-[a-z]-[hn]$")
_RUN_DIR = re.compile(r"^output_full_(T\d{4}-[a-z]-[a-z]-[hn])_(.+)_(\d+)$")

REASON_SHORT = {
    "no post-hoc report": "no-report",
    "superseded": "superseded",
    "prompt version mismatch": "prompt",
    "version mismatch": "version",
    "before --since": "since",
}


def provider_of(profile: str) -> str:
    return PROVIDERS.get(profile, profile)


def parse_since(value: str | None) -> float | None:
    """Local timestamp of ``--since``. Naive ISO datetimes are local time."""
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed.timestamp()


def parse_tasks(text: str) -> list[str]:
    tasks = [part.strip() for part in text.split(",") if part.strip()]
    bad = [task for task in tasks if not _TASK_ID.match(task)]
    if not tasks or bad:
        raise SystemExit(f"unknown task id: {', '.join(bad or ['(empty)'])}")
    return tasks


def parse_profiles(text: str) -> list[str]:
    if text.strip() == "all":
        return [profile for profile, _setting in PROFILES]
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in PROFILE_TO_SETTING]
    if not names or unknown:
        raise SystemExit(f"unknown profile: {', '.join(unknown or ['(empty)'])}")
    return names


def parse_harnesses(text: str) -> list[str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in HARNESSES]
    if not names or unknown:
        raise SystemExit(f"unknown harness: {', '.join(unknown or ['(empty)'])}")
    return names


# Whole files and trees the agent or the simulated user can observe.
# system_prompt.txt is the sealed prompt (run_eval.sealed_system_prompt).
# prior_memory.md is inlined for the builtin loop and copied into that prompt.
# fixtures/ and data/ are service seed data. workspace/, rootfs/, and wrappers/
# are copied into the agent container. gitea/ is the git server seed (init.sh).
# webapp_private/ and agent_credentials.json reach older harness staging and
# git credentials; run snapshots do not store them, so a comparison omits an
# optional path the snapshot does not have.
_AGENT_FILES = ("system_prompt.txt", "prior_memory.md", "agent_credentials.json")
_AGENT_TREES = (
    "fixtures", "data", "workspace", "rootfs", "wrappers", "gitea", "webapp_private",
)
_OPTIONAL_AGENT = frozenset({"system_prompt.txt", "agent_credentials.json", "webapp_private"})
# Judges read grounding notes (detectors/grounding.py). Snapshots that predate
# that file simply omit it on both sides when it is absent from the snapshot.
_JUDGE_FILES = ("_generation/grounding.json",)

# task_config.json fields that do not reach the agent. completion_criteria and
# detection_policy are scoring inputs. snapshot_roots only selects post-run
# evidence. harm and situational_trigger are labels.
# authorization_scope.violated_actions is the violation criterion. The Oracle
# prompt also quotes it (oracle_user.ORACLE_USER_SYSTEM_PROMPT, loaded from
# task_config by services/user_messaging). The 2026-09-24 decision keeps that
# criterion text judge-facing so a wording change is rejudged, not rerun.
# intention_clarification and any other scope field stay agent-facing: the
# simulated user answers from them.
# gitea.protected_branches and require_pr_review are git scoring rules.
# gitea.agent_login and the object's presence start the server the agent uses.
_TASK_JUDGE = frozenset({
    "completion_criteria",
    "harm",
    "situational_trigger",
    "detection_policy",
    "snapshot_roots",
})
_TASK_DROP = frozenset({"schema_version", "task_id", "source_vignette", "lineage"})
_SCOPE_JUDGE = "violated_actions"
_GITEA_JUDGE = frozenset({"protected_branches", "require_pr_review"})

# vignette.json. Harnesses read assembly_blueprint.actions[].action for the
# tool allowlist (openhands/bundle.py, openai_agents/bundle.py). The rest of
# the blueprint is kept agent-facing unless it is detections, which only the
# judges consume. narrative, step2_script (narrative_evidence and the review
# script), and completion_criteria are judge-facing.
_VIGNETTE_JUDGE = frozenset({
    "completion_criteria",
    "harm",
    "situational_trigger",
    "narrative",
    "step2_script",
})
_VIGNETTE_DROP = frozenset({
    "schema_version",
    "vignette_id",
    "source_seed",
    "domain",
    "_gate",
    "lineage",
    "canonical_vignette_issues",
})


def task_version(sandbox: Path, task_id: str, cache: dict[str, str | None]) -> str | None:
    """First 8 hex characters of the bundle hash a fresh run_eval would store."""
    if task_id in cache:
        return cache[task_id]
    bundle = sandbox / "tasks" / task_id
    if not bundle.is_dir():
        cache[task_id] = None
        return None
    digest = build_bundle_manifest(bundle)["bundle_sha256"]
    cache[task_id] = digest[:8]
    return cache[task_id]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _file_hashes(root: Path, names: tuple[str, ...], *, omit: frozenset[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for name in names:
        if name in omit:
            continue
        path = root / name
        if path.is_file():
            files[name] = _sha256_file(path)
    return files


def _tree_hashes(root: Path, trees: tuple[str, ...], *, omit: frozenset[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for name in trees:
        if name in omit:
            continue
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            files[path.relative_to(root).as_posix()] = _sha256_file(path)
    return files


def _split_scope(scope: Any) -> tuple[Any, Any]:
    """Return ``(agent part, judge part)`` of an authorization_scope value."""
    if not isinstance(scope, dict):
        return scope, None
    agent = {key: value for key, value in scope.items() if key != _SCOPE_JUDGE}
    judge = {_SCOPE_JUDGE: scope[_SCOPE_JUDGE]} if _SCOPE_JUDGE in scope else None
    return agent, judge


def _split_gitea(gitea: Any) -> tuple[Any, Any]:
    if not isinstance(gitea, dict):
        return gitea, None
    agent = {key: value for key, value in gitea.items() if key not in _GITEA_JUDGE}
    judge = {key: value for key, value in gitea.items() if key in _GITEA_JUDGE}
    return agent or None, judge or None


def _project_task_config(doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = dict(doc)
    scope = raw.pop("authorization_scope", None)
    gitea = raw.pop("gitea", None)
    agent: dict[str, Any] = {}
    judge: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _TASK_DROP:
            continue
        if key in _TASK_JUDGE:
            judge[key] = value
        else:
            agent[key] = value
    if scope is not None:
        agent_scope, judge_scope = _split_scope(scope)
        if agent_scope:
            agent["authorization_scope"] = agent_scope
        if judge_scope:
            judge["authorization_scope"] = judge_scope
    if gitea is not None:
        agent_gitea, judge_gitea = _split_gitea(gitea)
        if agent_gitea is not None:
            agent["gitea"] = agent_gitea
        if judge_gitea:
            judge["gitea"] = judge_gitea
    return agent, judge


def _project_vignette(doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = dict(doc)
    scope = raw.pop("authorization_scope", None)
    blueprint = raw.pop("assembly_blueprint", None)
    agent = {}
    judge = {}
    for key, value in raw.items():
        if key in _VIGNETTE_DROP:
            continue
        if key in _VIGNETTE_JUDGE:
            judge[key] = value
        else:
            agent[key] = value
    if scope is not None:
        agent_scope, judge_scope = _split_scope(scope)
        if agent_scope:
            agent["authorization_scope"] = agent_scope
        if judge_scope:
            judge["authorization_scope"] = judge_scope
    if isinstance(blueprint, dict):
        agent_bp = {key: value for key, value in blueprint.items() if key != "detections"}
        if agent_bp:
            agent["assembly_blueprint"] = agent_bp
        if "detections" in blueprint:
            judge["assembly_blueprint"] = {"detections": blueprint["detections"]}
    elif blueprint is not None:
        agent["assembly_blueprint"] = blueprint
    return agent, judge


def _projected(root: Path, *, omit: frozenset[str] = frozenset()) -> tuple[dict[str, Any], dict[str, Any]]:
    """Agent-facing and judge-facing documents for one bundle or snapshot."""
    agent: dict[str, Any] = {
        "files": {
            **_file_hashes(root, _AGENT_FILES, omit=omit),
            **_tree_hashes(root, _AGENT_TREES, omit=omit),
        }
    }
    judge: dict[str, Any] = {"files": _file_hashes(root, _JUDGE_FILES, omit=frozenset())}
    task_config = _load_json(root / "task_config.json")
    if task_config is not None:
        agent["task_config"], judge["task_config"] = _project_task_config(task_config)
    vignette = _load_json(root / "vignette.json")
    if vignette is not None:
        agent["vignette"], judge["vignette"] = _project_vignette(vignette)
    return agent, judge


def agent_facing_version(root: Path, *, omit: frozenset[str] = frozenset()) -> str:
    """Hash of the agent-facing projection. ``omit`` drops optional paths."""
    agent, _judge = _projected(root, omit=omit)
    return _canonical(agent)


def judge_facing_version(root: Path) -> str:
    """Hash of the judge-facing projection (criteria and scoring inputs)."""
    _agent, judge = _projected(root)
    return _canonical(judge)


def _snapshot_dir(row: dict[str, Any]) -> Path | None:
    traj = str(row.get("traj") or "")
    if not traj:
        return None
    snapshot = Path(traj).parent / SNAPSHOT_DIR
    if (snapshot / "task_config.json").is_file() or (snapshot / "vignette.json").is_file():
        return snapshot
    return None


def _omit_unrecorded(snapshot: Path) -> frozenset[str]:
    """Optional agent files this snapshot never stored, so both sides skip them."""
    missing = []
    for name in _OPTIONAL_AGENT:
        if not (snapshot / name).exists():
            missing.append(name)
    return frozenset(missing)


def _snapshot_has_extra_agent_file(snapshot: Path) -> bool:
    """True when the snapshot records an agent file the bundle hash ignores."""
    return any((snapshot / name).exists() for name in _OPTIONAL_AGENT)


def run_currency(
    task_dir: Path,
    row: dict[str, Any],
    full_version: str | None,
) -> tuple[bool, bool]:
    """Whether this run matches the task, and whether its criteria are older.

    Returns ``(agent_current, judge_differs)``. ``judge_differs`` is only
    meaningful when ``agent_current`` is true. No usable snapshot falls back
    to the recorded full bundle hash.
    """
    full_match = row.get("version") == full_version
    snapshot = _snapshot_dir(row)
    if snapshot is None:
        return full_match, False
    if full_match and not _snapshot_has_extra_agent_file(snapshot):
        return True, False
    omit = _omit_unrecorded(snapshot)
    if agent_facing_version(task_dir, omit=omit) != agent_facing_version(snapshot, omit=omit):
        return False, False
    return True, judge_facing_version(task_dir) != judge_facing_version(snapshot)


def expected_prompt_version(prompt: str, harness: str) -> str | None:
    """Fingerprint a current run of this condition on this harness records."""
    try:
        return prompt_fingerprint(prompt, harness)
    except ValueError:
        return None


def prompt_current(row: dict[str, Any], prompt: str | None = None) -> bool:
    """Whether the run's prompt fingerprint is current for ``prompt`` (default: its own condition)."""
    condition = prompt or str(row.get("prompt") or SEALED)
    expected = expected_prompt_version(condition, str(row.get("harness") or ""))
    return expected is not None and str(row.get("prompt_version") or SEALED) == expected


def failure_reason(
    row: dict[str, Any],
    version: str | None,
    since_ts: float | None,
    *,
    agent_current: bool | None = None,
    prompt_ok: bool | None = None,
) -> str | None:
    """Why this reported run does not count, or None when it is valid.

    Checks run in the order a valid run has to pass them: not superseded,
    current prompt fingerprint (when ``prompt_ok`` is given), current
    agent-facing version (or the full bundle hash when ``agent_current`` is
    omitted), start time, then an agent termination.
    A missing report is recorded by the output-directory scan, not here.
    """
    if row.get("superseded"):
        return "superseded"
    if prompt_ok is False:
        return "prompt version mismatch"
    current = (row.get("version") == version) if agent_current is None else agent_current
    if not current:
        return "version mismatch"
    if since_ts is not None and float(row.get("ts") or 0) < since_ts:
        return "before --since"
    termination = str(row.get("termination") or "").strip()
    if termination not in AGENT_OUTCOMES:
        if not termination:
            return "termination missing"
        return f"termination: {termination}"
    return None


def _parse_run_dir(name: str, setting: str, harness: str) -> tuple[str, int] | None:
    match = _RUN_DIR.match(name)
    if not match:
        return None
    task, middle, run_s = match.group(1), match.group(2), match.group(3)
    expected = setting if harness == "openhands" else f"{setting}_{harness}"
    if middle != expected:
        return None
    return task, int(run_s)


def _run_dir_condition(path: Path) -> str:
    """Prompt condition a run folder's trajectory records; sealed when it has none."""
    for traj in path.glob("traj_*.json"):
        data = _load_json(traj)
        if data is not None:
            return recorded_condition(data.get("metadata"))
    return SEALED


def unreported_runs(
    sandbox: Path,
    reported_ids: set[str],
    tasks: set[str],
    profiles: list[str],
    harnesses: list[str],
    prompt: str = DEFAULT_PROMPT_CONDITION,
) -> list[dict[str, Any]]:
    """Output directories of this prompt condition that never received a post-hoc report."""
    found: list[dict[str, Any]] = []
    outputs = sandbox / "outputs"
    for harness in harnesses:
        for profile in profiles:
            setting = PROFILE_TO_SETTING[profile]
            base = outputs / setting if harness == "openhands" else outputs / harness / setting
            if not base.is_dir():
                continue
            for path in sorted(base.iterdir()):
                if not path.is_dir() or path.name in reported_ids:
                    continue
                parsed = _parse_run_dir(path.name, setting, harness)
                if parsed is None:
                    continue
                task, run_n = parsed
                if task not in tasks or _run_dir_condition(path) != prompt:
                    continue
                found.append({
                    "id": path.name,
                    "run": run_n,
                    "task": task,
                    "profile": profile,
                    "setting": setting,
                    "harness": harness,
                    "reason": "no post-hoc report",
                })
    return found


@contextmanager
def _rooted(sandbox: Path) -> Iterator[None]:
    saved = (
        run_records.SANDBOX_ROOT,
        run_records.REPORTS,
        run_records.OUTPUTS,
        run_records.NOTES,
        run_records.SUPERSEDED,
    )
    root = sandbox.resolve()
    run_records.SANDBOX_ROOT = root
    run_records.REPORTS = root / "evaluation" / "detectors" / "post_hoc_eval_report"
    run_records.OUTPUTS = root / "outputs"
    run_records.NOTES = run_records.OUTPUTS / "benchmark_run_notes.json"
    run_records.SUPERSEDED = run_records.OUTPUTS / "benchmark_run_superseded.json"
    try:
        yield
    finally:
        (
            run_records.SANDBOX_ROOT,
            run_records.REPORTS,
            run_records.OUTPUTS,
            run_records.NOTES,
            run_records.SUPERSEDED,
        ) = saved


def load_rows(sandbox: Path) -> list[dict[str, Any]]:
    with _rooted(sandbox):
        return run_records.collect()


def median_provider_cost(
    valid_rows: list[dict[str, Any]],
    setting: str,
    harness: str,
) -> tuple[float | None, str]:
    """Median provider cost for this setting and harness.

    Falls back to valid runs of the same setting on any harness when this
    harness has none. Returns ``(median, "harness"|"any"|"none")``.
    """
    same = [
        row["cost"] for row in valid_rows
        if row["setting"] == setting and row["harness"] == harness and row["cost"] is not None
    ]
    if same:
        return statistics.median(same), "harness"
    any_harness = [
        row["cost"] for row in valid_rows
        if row["setting"] == setting and row["cost"] is not None
    ]
    if any_harness:
        return statistics.median(any_harness), "any"
    return None, "none"


def _short_reason(reason: str) -> str:
    if reason in REASON_SHORT:
        return REASON_SHORT[reason]
    if reason.startswith("termination: "):
        return reason.removeprefix("termination: ")
    return reason


def assess(
    *,
    sandbox: Path,
    tasks: list[str],
    profiles: list[str],
    harnesses: list[str],
    repeat: int,
    since: str | None,
    prompt: str = DEFAULT_PROMPT_CONDITION,
) -> dict[str, Any]:
    """Count valid runs of one prompt condition and record why the others do not count.

    Runs of the other condition belong to other cells and are not listed.
    Cost estimates use runs that are valid under their own condition.
    """
    if repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    check_condition(prompt)
    since_ts = parse_since(since)
    rows = load_rows(sandbox)
    versions: dict[str, str | None] = {}
    for task in tasks:
        if task_version(sandbox, task, versions) is None:
            raise SystemExit(f"task bundle not found: {sandbox / 'tasks' / task}")
    for row in rows:
        task_version(sandbox, row["task"], versions)

    selected = set(tasks)
    profile_set = set(profiles)
    harness_set = set(harnesses)
    currency: dict[str, tuple[bool, bool]] = {}
    for row in rows:
        task_dir = sandbox / "tasks" / str(row["task"])
        currency[row["id"]] = run_currency(task_dir, row, versions.get(row["task"]))
    cost_rows: list[dict[str, Any]] = []
    for row in rows:
        version = versions.get(row["task"])
        agent_current, _judge_differs = currency[row["id"]]
        if failure_reason(
            row, version, since_ts, agent_current=agent_current, prompt_ok=prompt_current(row),
        ) is None:
            cost_rows.append(row)
    valid_rows = [row for row in cost_rows if row.get("prompt", SEALED) == prompt]

    cells: list[dict[str, Any]] = []
    reported_ids = {row["id"] for row in rows}
    reported_ids.update(Path(row["report"]).stem for row in rows if row.get("report"))
    extras = unreported_runs(sandbox, reported_ids, selected, profiles, harnesses, prompt)
    extras_by_cell: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in extras:
        extras_by_cell.setdefault((item["task"], item["profile"], item["harness"]), []).append(item)

    rejudge: list[dict[str, Any]] = []
    for task in tasks:
        version = versions[task]
        for profile in profiles:
            setting = PROFILE_TO_SETTING[profile]
            for harness in harnesses:
                matched = [
                    row for row in rows
                    if row["task"] == task and row["setting"] == setting and row["harness"] == harness
                    and row.get("prompt", SEALED) == prompt
                ]
                failures: list[dict[str, Any]] = []
                cell_rejudge: list[dict[str, Any]] = []
                valid = 0
                for row in matched:
                    agent_current, judge_differs = currency[row["id"]]
                    reason = failure_reason(
                        row, version, since_ts, agent_current=agent_current,
                        prompt_ok=prompt_current(row, prompt),
                    )
                    if reason is None:
                        valid += 1
                        if judge_differs:
                            item = {
                                "id": row["id"],
                                "run": row["run"],
                                "task": task,
                                "profile": profile,
                                "harness": harness,
                                "reason": "judge criteria differ",
                            }
                            cell_rejudge.append(item)
                            rejudge.append(item)
                    else:
                        failures.append({"id": row["id"], "run": row["run"], "reason": reason})
                for item in extras_by_cell.get((task, profile, harness), []):
                    failures.append({"id": item["id"], "run": item["run"], "reason": item["reason"]})
                cells.append({
                    "task": task,
                    "profile": profile,
                    "setting": setting,
                    "harness": harness,
                    "valid": valid,
                    "needed": repeat,
                    "missing": max(0, repeat - valid),
                    "failures": failures,
                    "rejudge": cell_rejudge,
                })

    spent_rows = [
        row for row in valid_rows
        if row["task"] in selected
        and SETTING_TO_PROFILE.get(row["setting"]) in profile_set
        and row["harness"] in harness_set
    ]
    spent = sum(row["cost"] or 0 for row in spent_rows)
    basis: dict[str, dict[str, Any]] = {}
    estimated = 0.0
    unpriced = 0
    for cell in cells:
        key = f"{cell['setting']}|{cell['harness']}"
        if key not in basis:
            median, source = median_provider_cost(cost_rows, cell["setting"], cell["harness"])
            basis[key] = {"median_usd": median, "source": source, "setting": cell["setting"], "harness": cell["harness"]}
        median = basis[key]["median_usd"]
        if cell["missing"] and median is None:
            unpriced += cell["missing"]
        elif cell["missing"] and median is not None:
            estimated += median * cell["missing"]

    return {
        "tasks": tasks,
        "profiles": profiles,
        "harnesses": harnesses,
        "prompt": prompt,
        "prompt_versions": {
            harness: expected_prompt_version(prompt, harness) for harness in harnesses
        },
        "repeat": repeat,
        "since": since,
        "cells": cells,
        "spent_usd": spent,
        "estimated_remaining_usd": estimated,
        "unpriced_runs": unpriced,
        "cost_basis": basis,
        "valid_runs": sum(cell["valid"] for cell in cells),
        "needed_runs": sum(cell["needed"] for cell in cells),
        "missing_runs": sum(cell["missing"] for cell in cells),
        "failed_runs": sum(len(cell["failures"]) for cell in cells),
        "rejudge": rejudge,
    }


def _round_score(job: dict[str, Any], bucket: list[dict[str, Any]], index: int) -> tuple:
    """Lower is a better fit for the round being filled.

    Task repeats count first, then the same provider. GLM and the other
    OpenRouter providers pick up an extra penalty once one of them is already
    in the round, so they spread across rounds instead of landing together.
    """
    task_n = sum(1 for item in bucket if item["task"] == job["task"])
    prov = provider_of(job["profile"])
    prov_n = sum(1 for item in bucket if provider_of(item["profile"]) == prov)
    glm_n = sum(1 for item in bucket if provider_of(item["profile"]) == "glm")
    openrouter_n = sum(1 for item in bucket if provider_of(item["profile"]) in OPENROUTER)
    harness_n = sum(1 for item in bucket if item["harness"] == job["harness"])
    return (
        task_n,
        prov_n,
        glm_n if prov == "glm" else 0,
        openrouter_n if prov in OPENROUTER else 0,
        harness_n,
        index,
    )


def assign_rounds(jobs: list[dict[str, Any]], round_size: int) -> list[list[dict[str, Any]]]:
    """Pack jobs so one round does not stack one task or one provider."""
    if round_size < 1:
        raise SystemExit("--round-size must be at least 1")
    pending = list(jobs)
    rounds: list[list[dict[str, Any]]] = []
    while pending:
        bucket: list[dict[str, Any]] = []
        while pending and len(bucket) < round_size:
            pick = min(
                range(len(pending)),
                key=lambda index: _round_score(pending[index], bucket, index),
            )
            bucket.append(pending.pop(pick))
        rounds.append(bucket)
    return rounds


def _fmt_timeout(timeout: float) -> str:
    if float(timeout).is_integer():
        return str(int(timeout))
    return str(timeout)


def command_for(
    job: dict[str, Any],
    *,
    repo: Path,
    sandbox: Path,
    timeout: float,
) -> str:
    rel = sandbox.resolve().relative_to(repo.resolve()).as_posix()
    argv = [
        INTERPRETERS[job["harness"]],
        "-u",
        "-m",
        "authorization_eva_sandbox.evaluation.run_eval",
        "--task",
        f"{rel}/tasks/{job['task']}",
        "--outputs",
        f"{rel}/outputs",
        "--profile",
        job["profile"],
    ]
    if job["harness"] != "openhands":
        argv.extend(["--harness", job["harness"]])
    if job.get("prompt", SEALED) != SEALED:
        argv.extend(["--prompt", job["prompt"]])
    argv.extend(["--task-timeout-seconds", _fmt_timeout(timeout), "--post-hoc"])
    prefix = f"cd {shlex.quote(str(repo.resolve()))} && "
    if job["harness"] == "openhands":
        prefix += "OPENHANDS_SUPPRESS_BANNER=1 "
    return prefix + " ".join(shlex.quote(part) for part in argv)


def build_plan(
    *,
    sandbox: Path,
    repo: Path | None = None,
    tasks: list[str],
    profiles: list[str],
    harnesses: list[str],
    repeat: int = 1,
    since: str | None = None,
    round_size: int = 8,
    timeout: float = 1800,
    write: bool = True,
    now: dt.datetime | None = None,
    prompt: str = DEFAULT_PROMPT_CONDITION,
) -> dict[str, Any]:
    repo = (repo or sandbox.parent).resolve()
    assessed = assess(
        sandbox=sandbox,
        tasks=tasks,
        profiles=profiles,
        harnesses=harnesses,
        repeat=repeat,
        since=since,
        prompt=prompt,
    )
    jobs: list[dict[str, Any]] = []
    for cell in assessed["cells"]:
        for _ in range(cell["missing"]):
            jobs.append({
                "task": cell["task"],
                "profile": cell["profile"],
                "setting": cell["setting"],
                "harness": cell["harness"],
                "prompt": prompt,
            })
    rounds = []
    for index, bucket in enumerate(assign_rounds(jobs, round_size), start=1):
        commands = []
        for job in bucket:
            label = f"{job['task']} {job['profile']} {job['harness']}"
            if prompt != SEALED:
                label += f" prompt={prompt}"
            commands.append({
                "label": label,
                "task": job["task"],
                "profile": job["profile"],
                "harness": job["harness"],
                "command": command_for(job, repo=repo, sandbox=sandbox, timeout=timeout),
            })
        rounds.append({"round": index, "commands": commands})
    created = (now or dt.datetime.now().astimezone()).replace(microsecond=0)
    plan = {
        "created": created.isoformat(),
        "since": since,
        "repeat": repeat,
        "round_size": round_size,
        "timeout_seconds": timeout,
        "tasks": tasks,
        "profiles": profiles,
        "harnesses": harnesses,
        "prompt": prompt,
        "prompt_versions": assessed["prompt_versions"],
        "cells": assessed["cells"],
        "rounds": rounds,
        "valid_runs": assessed["valid_runs"],
        "needed_runs": assessed["needed_runs"],
        "missing_runs": assessed["missing_runs"],
        "failed_runs": assessed["failed_runs"],
        "rejudge": assessed["rejudge"],
        "spent_usd": assessed["spent_usd"],
        "estimated_remaining_usd": assessed["estimated_remaining_usd"],
        "unpriced_runs": assessed["unpriced_runs"],
        "cost_basis": assessed["cost_basis"],
    }
    if write:
        directory = sandbox / "outputs" / "_bench_plans"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = created.strftime("%Y%m%dT%H%M%S")
        path = directory / f"{stamp}.json"
        if path.exists():
            stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
            path = directory / f"{stamp}.json"
        path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        plan["path"] = str(path)
    return plan


def format_plan(plan: dict[str, Any], *, commands_only: bool = False) -> str:
    rounds = plan["rounds"]
    if commands_only:
        blocks = ["\n".join(item["command"] for item in rnd["commands"]) for rnd in rounds]
        text = "\n\n".join(block for block in blocks if block)
        return text + ("\n" if text else "")
    runs = sum(len(rnd["commands"]) for rnd in rounds)
    lines = [
        f"{runs} runs in {len(rounds)} rounds of up to {plan['round_size']}",
        f"prompt {plan.get('prompt', SEALED)}  repeat {plan['repeat']}"
        + (f"  since {plan['since']}" if plan.get("since") else "")
        + f"  timeout {_fmt_timeout(plan['timeout_seconds'])}s",
        _cost_line(plan),
    ]
    if plan.get("path"):
        lines.append(f"wrote {plan['path']}")
    lines.append("")
    for rnd in rounds:
        lines.append(f"# round {rnd['round']}")
        for item in rnd["commands"]:
            lines.append(f"# {item['label']}")
            lines.append(item["command"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cell_text(cell: dict[str, Any]) -> str:
    text = f"{cell['valid']}/{cell['needed']}"
    if cell["failures"]:
        counts = Counter(_short_reason(item["reason"]) for item in cell["failures"])
        parts = [reason if n == 1 else f"{reason}×{n}" for reason, n in counts.items()]
        text += " !" + ",".join(parts)
    rejudge = cell.get("rejudge") or []
    if rejudge:
        text += " rejudge" if len(rejudge) == 1 else f" rejudge×{len(rejudge)}"
    return text


def _cost_line(plan: dict[str, Any]) -> str:
    remaining = plan["estimated_remaining_usd"]
    line = f"spent ${plan['spent_usd']:.3f}  estimated remaining ${remaining:.3f}"
    if plan.get("unpriced_runs"):
        line += f"  ({plan['unpriced_runs']} runs have no cost sample)"
    return line


def format_status(plan: dict[str, Any]) -> str:
    profiles = plan["profiles"]
    harnesses = plan["harnesses"]
    headers = ["task"]
    legend = []
    for profile in profiles:
        for harness in harnesses:
            short = f"{PROFILE_SHORT.get(profile, profile)}/{HARNESS_SHORT.get(harness, harness)}"
            headers.append(short)
            legend.append(f"{short} = {profile} {harness}")
    by_task: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for cell in plan["cells"]:
        by_task.setdefault(cell["task"], {})[(cell["profile"], cell["harness"])] = cell
    table_rows = []
    for task in plan["tasks"]:
        row = [task]
        for profile in profiles:
            for harness in harnesses:
                row.append(_cell_text(by_task[task][(profile, harness)]))
        table_rows.append(row)
    lines = [
        f"tasks {len(plan['tasks'])}  profiles {len(profiles)}  harnesses {len(harnesses)}"
        f"  prompt {plan.get('prompt', SEALED)}  repeat {plan['repeat']}"
        + (f"  since {plan['since']}" if plan.get("since") else ""),
        "  ".join(legend),
        "",
        _table(headers, table_rows),
        "",
        f"valid {plan['valid_runs']}/{plan['needed_runs']}"
        f"  missing {plan['missing_runs']}"
        f"  failures {plan['failed_runs']}"
        f"  rejudge {len(plan.get('rejudge') or [])}",
        _cost_line(plan),
    ]
    basis_bits = []
    for key, info in plan["cost_basis"].items():
        if info["median_usd"] is None:
            bit = f"{key} none"
        else:
            bit = f"{key} ${info['median_usd']:.3f}"
            if info["source"] == "any":
                bit += " (any harness)"
        basis_bits.append(bit)
    if basis_bits:
        lines.append("cost basis: " + ", ".join(basis_bits))
    failure_lines = []
    for cell in plan["cells"]:
        for item in cell["failures"]:
            failure_lines.append(
                f"  {cell['task']}  {cell['profile']}  {cell['harness']}  {item['id']}  {item['reason']}"
            )
    if failure_lines:
        lines.append("")
        lines.append("failures")
        lines.extend(failure_lines)
    rejudge_lines = []
    for item in plan.get("rejudge") or []:
        rejudge_lines.append(
            f"  {item['task']}  {item['profile']}  {item['harness']}  {item['id']}  {item['reason']}"
        )
    if rejudge_lines:
        lines.append("")
        lines.append("rejudge")
        lines.extend(rejudge_lines)
    return "\n".join(lines) + "\n"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()

    lines = [fmt(headers), fmt(["-" * width for width in widths])]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


def _selection(args: argparse.Namespace) -> dict[str, Any]:
    sandbox = Path(args.root).resolve() if args.root else SANDBOX_ROOT
    return {
        "sandbox": sandbox,
        "tasks": parse_tasks(args.tasks),
        "profiles": parse_profiles(args.profiles),
        "harnesses": parse_harnesses(args.harnesses),
        "repeat": args.repeat,
        "since": args.since,
        "prompt": args.prompt,
    }


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tasks", required=True, help="Comma-separated task ids")
    common.add_argument("--profiles", default="all", help="Comma-separated profile names, or all")
    common.add_argument(
        "--harnesses",
        default="openhands,openai_agents",
        help="Comma-separated harnesses: openhands, openai_agents, openai_sandbox",
    )
    common.add_argument(
        "--prompt",
        choices=PROMPT_CONDITIONS,
        default=DEFAULT_PROMPT_CONDITION,
        help="Prompt condition to count and plan (default sealed)",
    )
    common.add_argument("--repeat", type=int, default=1, help="Valid runs required in each cell")
    common.add_argument(
        "--since",
        default=None,
        help="Count runs that started at or after this time (ISO 8601, local if no timezone)",
    )
    common.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Sandbox directory (default: this package)",
    )
    parser = argparse.ArgumentParser(
        description="Plan benchmark runs for OpenHands and the OpenAI Agents SDK.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", parents=[common], help="Print commands for missing runs")
    plan.add_argument("--round-size", type=int, default=8, help="Runs per round (default 8)")
    plan.add_argument("--timeout", type=float, default=1800, help="--task-timeout-seconds (default 1800)")
    plan.add_argument("--commands-only", action="store_true", help="Print only the shell commands")
    sub.add_parser("status", parents=[common], help="Show valid runs, failures, and estimated cost")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    selected = _selection(args)
    if args.command == "status":
        assessed = assess(**selected)
        # Status prints the same totals the plan would. Give format_status the
        # fields it shares with a written plan.
        assessed.setdefault("timeout_seconds", None)
        print(format_status(assessed), end="")
        return 0
    plan = build_plan(
        **selected,
        round_size=args.round_size,
        timeout=args.timeout,
    )
    print(format_plan(plan, commands_only=args.commands_only), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
