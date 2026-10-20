"""Versioned gate receipts for canonical generation artifacts.

One canonical artifact exists per scenario and stage. A stored pass is reusable
only when its gate contract, complete upstream inputs, and model-role profiles
match the current invocation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


RECEIPT_SCHEMA_VERSION = 1

GATE_CONTRACT_VERSIONS: dict[str, str] = {
    "1": "stage1-v4-clean-1",
    "2a": "stage2a-v4-clean-1",
    "2b": "stage2b-v4-clean-2",
    "2c": "stage2c-v4-clean-2",
    "3": "stage3-v4-clean-4",
    "4": "stage4-v4-clean-1",
    "5": "stage5-v4-clean-2",
}


def content_sha256(value: Any) -> str:
    """Hash JSON-like data canonically, or hash Path bytes exactly."""
    if isinstance(value, Path):
        payload = value.read_bytes()
    else:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def input_hashes(inputs: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(name): content_sha256(value)
        for name, value in sorted(inputs.items())
    }


def make_gate_receipt(
    *,
    stage: str,
    passed: bool,
    inputs: Mapping[str, Any],
    models: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if stage not in GATE_CONTRACT_VERSIONS:
        raise ValueError(f"unknown gate stage {stage!r}")
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "stage": stage,
        "contract_version": GATE_CONTRACT_VERSIONS[stage],
        "passed": bool(passed),
        "inputs": input_hashes(inputs),
        "models": {
            str(role): str(profile)
            for role, profile in sorted((models or {}).items())
        },
    }


def attach_gate_receipt(
    artifact: dict[str, Any],
    *,
    stage: str,
    passed: bool,
    inputs: Mapping[str, Any],
    models: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    artifact["_gate"] = make_gate_receipt(
        stage=stage,
        passed=passed,
        inputs=inputs,
        models=models,
    )
    return artifact


def gate_receipt_matches(
    artifact: Mapping[str, Any],
    *,
    stage: str,
    inputs: Mapping[str, Any],
    models: Mapping[str, str] | None = None,
) -> bool:
    receipt = artifact.get("_gate")
    if not isinstance(receipt, Mapping):
        return False
    expected = make_gate_receipt(
        stage=stage,
        passed=True,
        inputs=inputs,
        models=models,
    )
    return dict(receipt) == expected


def stored_gate_passed(
    artifact: Mapping[str, Any],
    *,
    stage: str | None = None,
) -> bool:
    receipt = artifact.get("_gate")
    if not isinstance(receipt, Mapping) or receipt.get("passed") is not True:
        return False
    if stage is not None and receipt.get("stage") != stage:
        return False
    return True


def registry_contract_snapshot() -> dict[str, Any]:
    """Return the live registry surface relevant to Stage 3 decisions."""
    from services.registry import ServiceRegistry

    registry = ServiceRegistry.load()
    templates: dict[str, Any] = {}
    for name, spec in sorted(registry.templates.items()):
        templates[name] = {
            "category": spec.category,
            "description": spec.description,
            "converted": spec.converted,
            "capability_notes": spec.capability_notes,
            "actions": [
                action.to_dict() for action in spec.actions
            ],
            "capability_profiles": {
                profile: list(actions)
                for profile, actions in sorted(
                    spec.capability_profiles.items()
                )
            },
            "environment_actions": {
                name: contract.to_dict()
                for name, contract in sorted(
                    spec.environment_actions.items()
                )
            },
            "fixture_template": (
                spec.fixture_template.to_dict()
                if spec.fixture_template is not None else None
            ),
        }
    return {"templates": templates}
