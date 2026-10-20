"""Single Stage 4 orchestration: compile, stage, seal, and atomically promote."""
from __future__ import annotations

import copy
import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from authorization_eva_sandbox.evaluation.bundle_manifest import (
    build_bundle_manifest,
)
from authorization_eva_sandbox.task_generation.shared.artifact_gate import (
    attach_gate_receipt,
    content_sha256,
    registry_contract_snapshot,
    stored_gate_passed,
)
from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    FillEnvelope,
    build_fill_envelope,
    require_stage3_registry_match,
    stage3_registry_sha,
    unwrap_blueprint,
    unwrap_script,
)
from authorization_eva_sandbox.task_generation.shared.world_realization import (
    compile_world_realization,
    prepare_world_realization,
    validate_world_realization,
)


STAGE4_ATTEMPTS = ".stage4-attempts"
RAW_REALIZATION_NAME = "world_realization.raw.json"


def attempt_dir(out_root: Path, task_id: str, attempt_id: str) -> Path:
    return Path(out_root) / STAGE4_ATTEMPTS / task_id / attempt_id


def canonical_dir(out_root: Path, task_id: str) -> Path:
    return Path(out_root) / task_id


def promote_stage4_bundle(staging: Path, canonical: Path) -> None:
    """Atomically replace the canonical bundle with a passing staging tree."""
    staging = staging.resolve()
    canonical = canonical.resolve()
    canonical.parent.mkdir(parents=True, exist_ok=True)
    backup = canonical.with_name(canonical.name + ".prev")
    if backup.exists():
        shutil.rmtree(backup)
    if canonical.exists():
        canonical.rename(backup)
    try:
        staging.rename(canonical)
    except OSError:
        if backup.exists() and not canonical.exists():
            backup.rename(canonical)
        raise
    if backup.exists():
        shutil.rmtree(backup)


STAGE4_RECEIPT_ARTIFACTS = (
    "compiler_report.json",
    "baseline_state.json",
    "grounding.json",
    "bindings.json",
    "world_review.json",
    "validation.json",
    "binding_report.json",
    "boot_report.json",
    "leak_report.json",
)


def bundle_task_id(bundle: Path) -> str:
    config_path = bundle / "task_config.json"
    if not config_path.is_file():
        raise SystemExit(f"{bundle}: missing task_config.json")
    config = json.loads(config_path.read_text())
    task_id = str(config.get("task_id") or "").strip()
    if not task_id:
        raise SystemExit(f"{bundle}: task_config.json is missing task_id")
    return task_id


def stage4_receipt_inputs(
    bundle: Path,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Receipt inputs for sealed artifacts. Does not include bundle_manifest."""
    inputs = dict(extra or {})
    generation = bundle / "_generation"
    for name in STAGE4_RECEIPT_ARTIFACTS:
        path = generation / name
        if path.is_file():
            inputs[name.removesuffix(".json")] = json.loads(path.read_text())
    return inputs


def require_stage4_receipt(bundle: Path) -> dict[str, Any]:
    manifest_path = bundle / "_generation" / "bundle_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"{bundle}: missing Stage 4 bundle_manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if not stored_gate_passed(manifest, stage="4"):
        raise SystemExit(f"{bundle}: Stage 4 receipt is absent or stale")
    if manifest.get("task_id") != bundle.name:
        raise SystemExit(
            f"{bundle}: Stage 4 manifest task_id "
            f"{manifest.get('task_id')!r} does not match bundle {bundle.name!r}"
        )
    current = build_bundle_manifest(bundle)
    current["task_id"] = bundle_task_id(bundle)
    if (
        manifest.get("files") != current["files"]
        or manifest.get("bundle_sha256") != current["bundle_sha256"]
    ):
        raise SystemExit(
            f"{bundle}: Stage 4 bundle hashes do not match current files"
        )
    return manifest


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def persist_stage4_text_audit(
    staging: Path, relative_path: str, content: str
) -> None:
    path = staging / "_generation" / "audits" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content))


def persist_stage4_json_audit(
    staging: Path, relative_path: str, value: Any
) -> None:
    _write_json(staging / "_generation" / "audits" / relative_path, value)


def begin_stage4_attempt(
    out_root: Path,
    task_id: str,
    *,
    attempt_id: str | None = None,
    realization: Mapping[str, Any] | None = None,
) -> Path:
    """Create the attempt tree and persist a parseable dump before later gates."""
    attempt_id = attempt_id or time.strftime("%Y%m%dT%H%M%S")
    staging = attempt_dir(out_root, task_id, attempt_id)
    (staging / "_generation").mkdir(parents=True, exist_ok=True)
    if realization is not None:
        persist_parseable_realization(staging, realization)
    return staging


def persist_parseable_realization(
    staging: Path,
    realization: Mapping[str, Any],
    *,
    include_raw: bool = True,
) -> None:
    payload = dict(realization)
    _write_json(staging / "_generation" / "world_realization.json", payload)
    if include_raw:
        _write_json(staging / "_generation" / RAW_REALIZATION_NAME, payload)


def persist_stage4_validation(staging: Path, issues: list[str]) -> None:
    _write_json(
        staging / "_generation" / "validation.json",
        {"issues": list(issues)},
    )


def persist_stage4_binding_report(
    staging: Path, report: Mapping[str, Any]
) -> None:
    _write_json(staging / "_generation" / "binding_report.json", dict(report))


def _runtime_stage_path(staging: Path, virtual: str) -> Path:
    """Stage /workspace paths in the workspace and other absolute paths in rootfs."""
    text = str(virtual)
    if text == "/workspace" or text.startswith("/workspace/"):
        relative = text.removeprefix("/workspace/").lstrip("/")
        return staging / "workspace" / relative
    return staging / "rootfs" / text.lstrip("/")


def write_compiled_bundle(
    *,
    staging: Path,
    seed: Mapping[str, Any],
    stem: str,
    policy_skeleton: Mapping[str, Any],
    script: Mapping[str, Any],
    surface: Mapping[str, Any],
    blueprint: Mapping[str, Any],
    canonical_vignette: Mapping[str, Any] | None,
    envelope: FillEnvelope,
    realization: Mapping[str, Any],
    fixtures: Mapping[str, Mapping[str, Any]],
    runtime: Mapping[str, Any],
    events: list[dict[str, Any]],
    reports: Mapping[str, Any],
) -> list[str]:
    from authorization_eva_sandbox.task_generation.stages.materialize.stage4 import (
        _expand_tree_spec,
        build_bindings,
        build_task_config,
        build_manifest,
    )

    raw_path = staging / "_generation" / RAW_REALIZATION_NAME
    preserved_raw = raw_path.read_text() if raw_path.is_file() else None
    audit_root = staging / "_generation" / "audits"
    preserved_audits = {
        path.relative_to(audit_root): path.read_bytes()
        for path in audit_root.rglob("*")
        if path.is_file()
    } if audit_root.is_dir() else {}
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    generation = staging / "_generation"
    generation.mkdir()
    if preserved_raw is not None:
        (generation / RAW_REALIZATION_NAME).write_text(preserved_raw)
    for relative, payload in preserved_audits.items():
        destination = generation / "audits" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    blueprint_body = unwrap_blueprint(blueprint)
    script_body = unwrap_script(script)
    surface_body = surface.get("agent_surface", surface)
    config = build_task_config(
        task_id="T" + stem[1:],
        stem=stem,
        seed=dict(seed),
        policy_skeleton=dict(policy_skeleton),
        surface=dict(surface_body),
        blueprint=blueprint_body,
    )
    config["instruction"] = envelope.instruction
    config["environment_events"] = list(events)
    if canonical_vignette is not None:
        _write_json(staging / "vignette.json", dict(canonical_vignette))
    fixtures_dir = staging / "fixtures"
    if fixtures:
        fixtures_dir.mkdir()
    for name, fixture in fixtures.items():
        _write_json(fixtures_dir / f"{name}.json", fixture)
    for spec in runtime.get("tree_specs") or []:
        expanded = dict(spec)
        if not expanded.get("extensions"):
            expanded["extensions"] = [".md"]
        if not expanded.get("root"):
            expanded["root"] = "/workspace/project"
        if int(expanded.get("total_files") or 0) < len(expanded.get("key_files") or []):
            expanded["total_files"] = len(expanded.get("key_files") or [])
        stage_fn = lambda path, staging=staging: _runtime_stage_path(staging, path)
        _expand_tree_spec(expanded, stage_fn)
        root = str(expanded.get("root", "")).rstrip("/")
        for kf in expanded.get("key_files") or []:
            mtime = kf.get("mtime_iso")
            rel = str(kf.get("path", "")).lstrip("/")
            if not mtime or not rel:
                continue
            try:
                from datetime import datetime as _dt

                when = _dt.fromisoformat(str(mtime))
                dest = stage_fn(f"{root}/{rel}")
                if dest.is_file():
                    import os

                    epoch = when.timestamp()
                    os.utime(dest, (epoch, epoch))
            except (OSError, ValueError):
                pass
    for item in runtime.get("workspace_files") or []:
        dest = _runtime_stage_path(staging, str(item.get("path") or ""))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(str(item.get("content") or ""))
    memory = str(runtime.get("prior_memory_md") or "")
    if memory:
        (staging / "prior_memory.md").write_text(memory)
        config["prior_memory_file"] = "prior_memory.md"
    from services.registry import ServiceRegistry
    registry = ServiceRegistry.load()
    manifest = build_manifest(blueprint_body, script_body, registry)
    bindings = build_bindings(blueprint_body, dict(policy_skeleton))
    _write_json(staging / "task_config.json", config)
    _write_json(generation / "manifest.json", {"materials": manifest})
    _write_json(generation / "bindings.json", {"bindings": bindings})
    _write_json(generation / "fill_envelope.json", envelope.to_dict())
    _write_json(generation / "world_realization.json", dict(realization))
    _write_json(generation / "compiler_report.json", dict(reports))
    return []


def persist_stage4_audit(bundle: Path, audit: Mapping[str, Any]) -> None:
    _write_json(bundle / "_generation" / "world_review.json", dict(audit))


def persist_stage4_leak_report(bundle: Path, report: Mapping[str, Any]) -> None:
    _write_json(bundle / "_generation" / "leak_report.json", dict(report))


def load_stage4_oracle_facts(bundle: Path) -> list[Any]:
    path = bundle / "_generation" / "grounding.json"
    if not path.is_file():
        return []
    facts = json.loads(path.read_text()).get("oracle_facts") or []
    return list(facts) if isinstance(facts, list) else []


def stage4_seal_blockers(bundle: Path) -> list[str]:
    """Reasons a passing Stage 4 receipt cannot be sealed."""
    generation = bundle / "_generation"
    blockers: list[str] = []
    validation_path = generation / "validation.json"
    if not validation_path.is_file():
        blockers.append("missing validation.json")
    else:
        issues = json.loads(validation_path.read_text()).get("issues") or []
        if issues:
            blockers.append("validation issues remain")
    required_pass = (
        ("boot_report.json", "boot"),
        ("world_review.json", "world review"),
        ("leak_report.json", "leak"),
    )
    for name, label in required_pass:
        path = generation / name
        if not path.is_file():
            blockers.append(f"missing {name}")
            continue
        document = json.loads(path.read_text())
        if document.get("pass") is not True:
            blockers.append(f"{label} did not pass")
    for name in ("bindings.json", "grounding.json"):
        if not (generation / name).is_file():
            blockers.append(f"missing {name}")
    return blockers


def write_synthetic_passing_gates(bundle: Path) -> None:
    """Inject explicit passing semantic/boot receipts for offline seal tests."""
    generation = bundle / "_generation"
    validation_path = generation / "validation.json"
    if validation_path.is_file():
        issues = json.loads(validation_path.read_text()).get("issues") or []
        if issues:
            raise SystemExit(
                "cannot inject passing Stage 4 gates over validation issues"
            )
    else:
        _write_json(validation_path, {"issues": []})
    _write_json(
        generation / "boot_report.json",
        {"pass": True, "failures": [], "skipped": []},
    )
    if not (generation / "grounding.json").is_file():
        _write_json(
            generation / "grounding.json",
            {"references": [], "oracle_facts": [], "realizations": []},
        )
    _write_json(
        generation / "world_review.json",
        {
            "schema_version": 1,
            "pass": True,
            "problems": [],
            "targets": [],
            "conditions": [],
            "repair": {
                "attempted": False,
                "base_sha256": "",
                "applied": False,
                "rolled_back": False,
                "replacements": [],
                "problems": [],
            },
        },
    )
    _write_json(
        generation / "leak_report.json",
        {
            "schema_version": 1,
            "pass": True,
            "problems": [],
            "batches": [],
            "findings": [],
        },
    )


def seal_stage4_bundle(
    bundle: Path,
    *,
    inputs: Mapping[str, Any],
    models: Mapping[str, str],
    passed: bool,
) -> dict[str, Any]:
    if passed:
        blockers = stage4_seal_blockers(bundle)
        if blockers:
            raise SystemExit(
                "Stage 4 cannot seal without passing validation, boot, "
                "world review, grounding/bindings, and leak reports: "
                + "; ".join(blockers)
            )
    manifest = build_bundle_manifest(bundle)
    manifest["task_id"] = bundle_task_id(bundle)
    attach_gate_receipt(
        manifest,
        stage="4",
        passed=passed,
        inputs=inputs,
        models=models,
    )
    _write_json(bundle / "_generation" / "bundle_manifest.json", manifest)
    return manifest


def collect_semantic_findings(
    review: Mapping[str, Any],
    leak: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    problems = [f"review: {item}" for item in (review.get("problems") or [])]
    problems.extend(f"leak: {item}" for item in (leak.get("problems") or []))
    findings: list[dict[str, Any]] = []
    for target in review.get("targets") or []:
        if not isinstance(target, Mapping):
            continue
        findings.extend(
            dict(item) for item in (target.get("findings") or [])
            if isinstance(item, Mapping)
        )
    findings.extend(
        dict(item) for item in (leak.get("findings") or [])
        if isinstance(item, Mapping)
    )
    return findings, problems


def _with_repair(
    review: Mapping[str, Any], repair: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(review)
    merged["repair"] = {
        "attempted": bool(repair.get("attempted")),
        "base_sha256": str(repair.get("base_sha256") or ""),
        "applied": bool(repair.get("applied")),
        "rolled_back": bool(repair.get("rolled_back")),
        "stopped": bool(repair.get("stopped")),
        "replacements": list(repair.get("replacements") or []),
        "problems": list(repair.get("problems") or []),
        "requests": copy.deepcopy(repair.get("requests") or []),
        "timeplan": copy.deepcopy(repair.get("timeplan") or {}),
        "values": copy.deepcopy(repair.get("values") or {}),
    }
    return merged


def _empty_repair_audit(realization: Mapping[str, Any]) -> dict[str, Any]:
    from authorization_eva_sandbox.task_generation.shared.world_realization import (
        realization_sha256,
    )

    return {
        "attempted": False,
        "applied": False,
        "rolled_back": False,
        "stopped": False,
        "base_sha256": realization_sha256(realization),
        "replacements": [],
        "problems": [],
        "requests": [],
    }


def _violated_actions(policy_skeleton: Mapping[str, Any]) -> list[Any]:
    scope = policy_skeleton.get("authorization_scope") or {}
    if not isinstance(scope, Mapping):
        return []
    actions = scope.get("violated_actions") or []
    return list(actions) if isinstance(actions, list) else []


def run_stage4_semantic_gates(
    *,
    staging: Path,
    envelope: FillEnvelope,
    realization: Mapping[str, Any],
    registry: Any,
    policy_skeleton: Mapping[str, Any],
    review_call: Callable[[str], Any],
    leak_call: Callable[[str], Any],
    fixtures: Mapping[str, Any] | None = None,
    runtime: Mapping[str, Any] | None = None,
    events: list[Mapping[str, Any]] | None = None,
    compiler_report: Mapping[str, Any] | None = None,
    oracle_facts: list[Any] | None = None,
    review_cleanup: Callable[[str], Any] | None = None,
    leak_cleanup: Callable[[str], Any] | None = None,
    repair: Mapping[str, Any] | None = None,
    bind: Callable[[], bool | None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stage 2 review, optional bind, then leak. Leak is the last semantic write."""
    from authorization_eva_sandbox.task_generation.shared.world_leak import (
        scan_world_leaks,
    )
    from authorization_eva_sandbox.task_generation.shared.world_verification import (
        review_compiled_world,
    )

    review = review_compiled_world(
        envelope=envelope,
        realization=realization,
        registry=registry,
        critic_call=review_call,
        cleanup_call=review_cleanup,
        fixtures=fixtures,
        runtime=runtime,
        events=events,
        compiler_report=compiler_report,
    )
    if repair is not None:
        review = _with_repair(review, repair)
    persist_stage4_audit(staging, review)
    if review.get("pass") is not True:
        leak = {
            "schema_version": 1,
            "realization_sha256": review.get("realization_sha256"),
            "pass": False,
            "skipped": True,
            "findings": [],
            "problems": [],
        }
        persist_stage4_leak_report(staging, leak)
        return review, leak
    facts = oracle_facts
    if bind is not None:
        bound = bind()
        if bound is False:
            review = dict(review)
            review["pass"] = False
            review["problems"] = list(review.get("problems") or []) + [
                "binding/grounding checks failed"
            ]
            persist_stage4_audit(staging, review)
            leak = {
                "schema_version": 1,
                "realization_sha256": review.get("realization_sha256"),
                "pass": False,
                "skipped": True,
                "findings": [],
                "problems": [],
            }
            persist_stage4_leak_report(staging, leak)
            return review, leak
        facts = load_stage4_oracle_facts(staging)
    leak = scan_world_leaks(
        envelope=envelope,
        realization=realization,
        registry=registry,
        violated_actions=_violated_actions(policy_skeleton),
        critic_call=leak_call,
        cleanup_call=leak_cleanup,
        fixtures=fixtures,
        runtime=runtime,
        events=events,
        oracle_facts=facts,
    )
    persist_stage4_leak_report(staging, leak)
    return review, leak


def run_stage4_repair_loop(
    *,
    staging: Path,
    envelope: FillEnvelope,
    realization: Mapping[str, Any],
    registry: Any,
    policy_skeleton: Mapping[str, Any],
    review_call: Callable[[str], Any],
    leak_call: Callable[[str], Any],
    repair_call: Callable[[str], Any],
    reference_time: str,
    time_repair_call: Callable[[str], Any] | None = None,
    fixtures: Mapping[str, Any] | None = None,
    runtime: Mapping[str, Any] | None = None,
    events: list[Mapping[str, Any]] | None = None,
    compiler_report: Mapping[str, Any] | None = None,
    oracle_facts: list[Any] | None = None,
    review_cleanup: Callable[[str], Any] | None = None,
    leak_cleanup: Callable[[str], Any] | None = None,
    recompile: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    bind: Callable[[], bool | None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """At most one replacement batch, then a full semantic rerun."""
    from authorization_eva_sandbox.task_generation.shared.world_repair import (
        repair_from_review,
    )
    from authorization_eva_sandbox.task_generation.shared.world_time_repair import (
        repair_timeplan_findings,
    )
    from authorization_eva_sandbox.task_generation.shared.world_realization import (
        realization_sha256,
    )

    current = dict(realization)
    current_fixtures = fixtures
    current_runtime = runtime
    current_events = events
    current_report = compiler_report
    current_facts = oracle_facts
    repair_audit = _empty_repair_audit(current)
    audit: dict[str, Any] = {
        "pass": False,
        "stopped": False,
        "problems": [],
        "repair": repair_audit,
        "review": None,
        "leak": None,
    }

    def _gates(active: Mapping[str, Any] | None = None):
        return run_stage4_semantic_gates(
            staging=staging,
            envelope=envelope,
            realization=current,
            registry=registry,
            policy_skeleton=policy_skeleton,
            review_call=review_call,
            leak_call=leak_call,
            fixtures=current_fixtures,
            runtime=current_runtime,
            events=current_events,
            compiler_report=current_report,
            oracle_facts=current_facts,
            review_cleanup=review_cleanup,
            leak_cleanup=leak_cleanup,
            repair=active,
            bind=bind,
        )

    review, leak = _gates()
    audit["review"] = review
    audit["leak"] = leak
    if review.get("pass") is True and leak.get("pass") is True:
        audit["pass"] = True
        return current, audit

    findings, problems = collect_semantic_findings(review, leak)
    if problems:
        audit["stopped"] = True
        audit["problems"] = problems
        return current, audit
    if not findings:
        audit["stopped"] = True
        audit["problems"] = ["semantic gates failed without repairable findings"]
        return current, audit

    time_patched, remaining, time_audit = repair_timeplan_findings(
        current,
        findings=findings,
        envelope=envelope,
        registry=registry,
        fixtures=current_fixtures,
        reference_time=reference_time,
        repair_call=time_repair_call or repair_call,
    )
    if time_audit.get("stopped"):
        audit["repair"] = time_audit
        persist_stage4_audit(staging, _with_repair(review, time_audit))
        audit["stopped"] = True
        audit["problems"] = list(time_audit.get("problems") or [])
        return current, audit
    if remaining:
        patched, value_audit = repair_from_review(
            time_patched,
            findings=remaining,
            envelope=envelope,
            registry=registry,
            repair_call=repair_call,
        )
    else:
        patched = time_patched
        value_audit = _empty_repair_audit(time_patched)
    repair_audit = {
        "attempted": bool(
            time_audit.get("attempted") or value_audit.get("attempted")
        ),
        "applied": bool(
            (time_audit.get("applied") or value_audit.get("applied"))
            and not value_audit.get("stopped")
        ),
        "rolled_back": bool(
            time_audit.get("rolled_back") or value_audit.get("rolled_back")
        ),
        "stopped": bool(
            time_audit.get("stopped") or value_audit.get("stopped")
        ),
        "base_sha256": realization_sha256(current),
        "replacements": list(time_audit.get("replacements") or [])
        + list(value_audit.get("replacements") or []),
        "problems": list(time_audit.get("problems") or [])
        + list(value_audit.get("problems") or []),
        "requests": list(time_audit.get("requests") or [])
        + list(value_audit.get("requests") or []),
        "timeplan": time_audit,
        "values": value_audit,
    }
    if value_audit.get("stopped") or (
        remaining and not value_audit.get("applied")
    ):
        patched = current
        repair_audit["applied"] = False
        repair_audit["rolled_back"] = True
    audit["repair"] = repair_audit
    persist_stage4_audit(staging, _with_repair(review, repair_audit))
    if not repair_audit.get("applied"):
        audit["stopped"] = True
        audit["problems"] = list(
            repair_audit.get("problems") or ["repair did not apply"]
        )
        return current, audit
    if recompile is None:
        audit["stopped"] = True
        audit["problems"] = ["repair applied but recompile callback is missing"]
        return patched, audit

    compiled = dict(recompile(patched) or {})
    if compiled.get("pass") is False:
        audit["stopped"] = True
        audit["problems"] = list(compiled.get("issues") or ["recompile failed"])
        return patched, audit
    current = dict(compiled.get("realization") or patched)
    current_fixtures = compiled.get("fixtures", current_fixtures)
    current_runtime = compiled.get("runtime", current_runtime)
    current_events = compiled.get("events", current_events)
    current_report = compiled.get("compiler_report", current_report)
    current_facts = compiled.get("oracle_facts", current_facts)

    review, leak = _gates(repair_audit)
    audit["review"] = review
    audit["leak"] = leak
    if review.get("pass") is True and leak.get("pass") is True:
        audit["pass"] = True
        return current, audit
    audit["stopped"] = True
    audit["problems"] = ["second semantic pass failed after replacement"]
    findings, problems = collect_semantic_findings(review, leak)
    audit["problems"].extend(problems)
    if findings:
        audit["problems"].extend(
            str(item.get("problem") or item.get("where") or "finding")
            for item in findings
        )
    return current, audit


def materialize_world_offline(
    *,
    out_root: Path,
    seed: Mapping[str, Any],
    stem: str,
    policy_skeleton: Mapping[str, Any],
    script: Mapping[str, Any],
    surface: Mapping[str, Any],
    blueprint: Mapping[str, Any],
    canonical_vignette: Mapping[str, Any] | None = None,
    realization: Mapping[str, Any],
    registry: Any,
    attempt_id: str | None = None,
    promote: bool = True,
    seal: bool | None = None,
    inject_passing_gates: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Compile a provided realization into a staged bundle. No LLM calls."""
    if stage3_registry_sha(blueprint) is not None:
        require_stage3_registry_match(blueprint)
    envelope = build_fill_envelope(
        scenario_id=stem,
        policy_skeleton=policy_skeleton,
        script=script,
        blueprint=blueprint,
        registry=registry,
        upstream_hashes=(
            {"vignette": content_sha256(canonical_vignette)}
            if canonical_vignette is not None
            else {
                "policy_skeleton": content_sha256(policy_skeleton),
                "script": content_sha256(script),
                "blueprint": content_sha256(blueprint),
            }
        ),
        registry_snapshot=registry_contract_snapshot(),
    )
    candidate = copy.deepcopy(dict(realization))
    binding_report = prepare_world_realization(
        candidate, envelope, registry=registry
    )
    issues = validate_world_realization(
        candidate, envelope=envelope, registry=registry, prepare=False
    )
    principal = dict(policy_skeleton.get("oracle_user") or {})
    fixtures, runtime, events, reports, compile_issues = compile_world_realization(
        candidate,
        envelope=envelope,
        registry=registry,
        reference_time=str(
            policy_skeleton.get("reference_time") or "2026-06-06T10:11:00"
        ),
        principal=principal,
    )
    issues.extend(compile_issues)
    task_id = "T" + stem[1:]
    attempt_id = attempt_id or time.strftime("%Y%m%dT%H%M%S")
    staging = attempt_dir(out_root, task_id, attempt_id)
    write_compiled_bundle(
        staging=staging,
        seed=seed,
        stem=stem,
        policy_skeleton=policy_skeleton,
        script=script,
        surface=surface,
        blueprint=blueprint,
        canonical_vignette=canonical_vignette,
        envelope=envelope,
        realization=candidate,
        fixtures=fixtures,
        runtime=runtime,
        events=events,
        reports=reports,
    )
    _write_json(staging / "_generation" / "validation.json", {"issues": issues})
    persist_stage4_binding_report(staging, binding_report)
    compiled = {
        "pass": not issues,
        "issues": issues,
        "attempt": attempt_id,
        "envelope": envelope,
        "realization": candidate,
        "fixtures": fixtures,
        "runtime": runtime,
        "events": events,
        "compiler_report": reports,
        "binding_report": binding_report,
    }
    passed = not issues
    if seal is None:
        seal = promote
    if inject_passing_gates:
        if not passed:
            return staging, compiled
        write_synthetic_passing_gates(staging)
    if passed and seal:
        blockers = stage4_seal_blockers(staging)
        if blockers:
            compiled["pass"] = False
            compiled["issues"] = list(issues) + blockers
            return staging, compiled
        seal_stage4_bundle(
            staging,
            inputs=stage4_receipt_inputs(
                staging,
                {
                    **(
                        {"vignette": canonical_vignette}
                        if canonical_vignette is not None
                        else {
                            "policy_skeleton": policy_skeleton,
                            "script": script,
                            "blueprint": blueprint,
                        }
                    ),
                    "registry": registry_contract_snapshot(),
                    "fill_envelope": envelope.to_dict(),
                    "world_realization": candidate,
                },
            ),
            models={"generator": "offline", "critic": "offline"},
            passed=True,
        )
        canonical = canonical_dir(out_root, task_id)
        if promote:
            promote_stage4_bundle(staging, canonical)
            compiled["issues"] = []
            return canonical, compiled
        return staging, compiled
    return staging, compiled
