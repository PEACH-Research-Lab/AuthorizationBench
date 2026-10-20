"""Canonical, complete vignette assembled at the Stage 3 success boundary.

The split Stage 1/2/3 documents are generation-process artifacts.  The
repository-level ``vignettes/V*.json`` document is the single case-design
artifact consumed by Stage 4 and copied into sealed task bundles.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from authorization_eva_sandbox.task_generation.shared.artifact_gate import (
    attach_gate_receipt,
    registry_contract_snapshot,
    stored_gate_passed,
)
from authorization_eva_sandbox.task_generation.shared.utils import (
    atomic_write_json,
    read_json,
)


CANONICAL_VIGNETTE_SCHEMA_VERSION = 1

_POLICY_FIELDS = (
    "source_seed",
    "situational_trigger",
    "domain",
    "harm",
    "instruction",
    "completion_criteria",
    "authorization_scope",
    "oracle_user",
    "reference_time",
)

_SECTION_FIELDS = (
    "step2_script",
    "agent_surface",
    "assembly_blueprint",
)

_SOURCE_STAGES = {
    "policy_skeleton": "1",
    "story": "2a",
    "script": "2b",
    "surface": "2c",
    "blueprint": "3",
}


class CanonicalVignetteError(ValueError):
    """Raised when split artifacts cannot form one canonical vignette."""


def canonical_vignette_path(vignettes_dir: Path, stem: str) -> Path:
    return vignettes_dir / f"{stem}.json"


def split_vignette_paths(vignettes_dir: Path, stem: str) -> dict[str, Path]:
    return {
        "policy_skeleton": vignettes_dir / "_policy_skeletons" / f"{stem}.json",
        "story": vignettes_dir / "_stories" / f"{stem}.json",
        "script": vignettes_dir / "_scripts" / f"{stem}.json",
        "surface": vignettes_dir / "_surfaces" / f"{stem}.json",
        "blueprint": vignettes_dir / "_blueprints" / f"{stem}.json",
    }


def load_split_vignette_documents(
    vignettes_dir: Path,
    stem: str,
) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for name, path in split_vignette_paths(vignettes_dir, stem).items():
        if not path.is_file():
            raise CanonicalVignetteError(
                f"missing {name} process artifact for {stem}: {path}"
            )
        document = read_json(path)
        if not isinstance(document, dict):
            raise CanonicalVignetteError(
                f"{name} process artifact for {stem} must be an object"
            )
        documents[name] = document
    return documents


def assemble_canonical_vignette(
    *,
    stem: str,
    policy_skeleton: Mapping[str, Any],
    story: Mapping[str, Any],
    script: Mapping[str, Any],
    surface: Mapping[str, Any],
    blueprint: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge passing Stage 1/2/3 artifacts without changing their semantics."""
    sources = {
        "policy_skeleton": policy_skeleton,
        "story": story,
        "script": script,
        "surface": surface,
        "blueprint": blueprint,
    }
    for name, stage in _SOURCE_STAGES.items():
        if not stored_gate_passed(sources[name], stage=stage):
            raise CanonicalVignetteError(
                f"{name} for {stem} does not have a passing Stage {stage} receipt"
            )

    narrative = story.get("narrative")
    step2_script = script.get("step2_script")
    agent_surface = surface.get("agent_surface")
    assembly_blueprint = blueprint.get("assembly_blueprint")
    if not isinstance(narrative, str) or not narrative.strip():
        raise CanonicalVignetteError(f"story for {stem} has no narrative")
    for name, value in (
        ("step2_script", step2_script),
        ("agent_surface", agent_surface),
        ("assembly_blueprint", assembly_blueprint),
    ):
        if not isinstance(value, Mapping):
            raise CanonicalVignetteError(f"{name} for {stem} must be an object")

    document: dict[str, Any] = {
        "schema_version": CANONICAL_VIGNETTE_SCHEMA_VERSION,
        "vignette_id": stem,
    }
    for field in _POLICY_FIELDS:
        if field in policy_skeleton:
            document[field] = copy.deepcopy(policy_skeleton[field])
    document.update({
        "narrative": narrative,
        "step2_script": copy.deepcopy(dict(step2_script)),
        "agent_surface": copy.deepcopy(dict(agent_surface)),
        "assembly_blueprint": copy.deepcopy(dict(assembly_blueprint)),
        "_gate": copy.deepcopy(dict(blueprint["_gate"])),
    })

    issues = canonical_vignette_issues(document, expected_stem=stem)
    if issues:
        raise CanonicalVignetteError("; ".join(issues))
    return document


def canonical_vignette_issues(
    document: Mapping[str, Any],
    *,
    expected_stem: str | None = None,
) -> list[str]:
    issues: list[str] = []
    if document.get("schema_version") != CANONICAL_VIGNETTE_SCHEMA_VERSION:
        issues.append(
            "unsupported canonical vignette schema_version "
            f"{document.get('schema_version')!r}"
        )
    vignette_id = document.get("vignette_id")
    if not isinstance(vignette_id, str) or not vignette_id:
        issues.append("vignette_id must be a non-empty string")
    elif expected_stem is not None and vignette_id != expected_stem:
        issues.append(
            f"vignette_id {vignette_id!r} does not match {expected_stem!r}"
        )

    for field in (
        "source_seed",
        "instruction",
        "narrative",
    ):
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{field} must be a non-empty string")
    for field in ("completion_criteria", "situational_trigger"):
        if not isinstance(document.get(field), list):
            issues.append(f"{field} must be a list")
    for field in ("authorization_scope", "oracle_user", *_SECTION_FIELDS):
        if not isinstance(document.get(field), Mapping):
            issues.append(f"{field} must be an object")

    blueprint = document.get("assembly_blueprint")
    if isinstance(blueprint, Mapping) and blueprint.get("unresolved"):
        issues.append("assembly_blueprint.unresolved must be empty")
    if not stored_gate_passed(document, stage="3"):
        issues.append("canonical vignette must have a passing Stage 3 receipt")
    for field in ("step2_script", "agent_surface", "assembly_blueprint"):
        value = document.get(field)
        if isinstance(value, Mapping) and "_gate" in value:
            issues.append(f"{field} must not contain a nested _gate")
    return issues


def canonical_vignette_sections(
    document: Mapping[str, Any],
    *,
    expected_stem: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return existing Stage 1/2/3 document views from one canonical vignette."""
    issues = canonical_vignette_issues(document, expected_stem=expected_stem)
    if issues:
        raise CanonicalVignetteError("; ".join(issues))
    policy_skeleton = {
        field: copy.deepcopy(document[field])
        for field in _POLICY_FIELDS
        if field in document
    }
    return {
        "policy_skeleton": policy_skeleton,
        "story": {"narrative": document["narrative"]},
        "script": {"step2_script": copy.deepcopy(document["step2_script"])},
        "surface": {"agent_surface": copy.deepcopy(document["agent_surface"])},
        "blueprint": {
            "assembly_blueprint": copy.deepcopy(
                document["assembly_blueprint"]
            ),
            "_gate": copy.deepcopy(document["_gate"]),
        },
    }


def promote_canonical_vignette(
    *,
    vignettes_dir: Path,
    stem: str,
    documents: Mapping[str, Mapping[str, Any]] | None = None,
) -> Path:
    """Atomically write ``vignettes/V*.json`` from passing process artifacts."""
    source_documents = (
        dict(documents)
        if documents is not None
        else load_split_vignette_documents(vignettes_dir, stem)
    )
    missing = sorted(set(_SOURCE_STAGES) - set(source_documents))
    if missing:
        raise CanonicalVignetteError(
            f"missing split documents for {stem}: {missing!r}"
        )
    document = assemble_canonical_vignette(
        stem=stem,
        policy_skeleton=source_documents["policy_skeleton"],
        story=source_documents["story"],
        script=source_documents["script"],
        surface=source_documents["surface"],
        blueprint=source_documents["blueprint"],
    )
    from authorization_eva_sandbox.task_generation.stages.assembly.stage3 import (
        stage3_contract_fingerprint,
    )

    blueprint_gate = source_documents["blueprint"].get("_gate") or {}
    attach_gate_receipt(
        document,
        stage="3",
        passed=True,
        inputs={
            "policy_skeleton": source_documents["policy_skeleton"],
            "story": source_documents["story"],
            "script": source_documents["script"],
            "surface": source_documents["surface"],
            "registry": registry_contract_snapshot(),
            "prompt_contract": stage3_contract_fingerprint(),
        },
        models=dict(blueprint_gate.get("models") or {}),
    )
    target = canonical_vignette_path(vignettes_dir, stem)
    atomic_write_json(target, document)
    discard_split_vignette_documents(vignettes_dir, stem)
    return target


def discard_split_vignette_documents(vignettes_dir: Path, stem: str) -> list[Path]:
    """Remove Stage 1/2/3 process copies after the canonical vignette exists."""
    removed: list[Path] = []
    for path in split_vignette_paths(vignettes_dir, stem).values():
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed
