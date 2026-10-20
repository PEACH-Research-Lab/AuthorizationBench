"""Machine-readable semantic contracts layered on live OpenAPI actions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


ActionClass = Literal["core", "extension", "environment_only"]


@dataclass(frozen=True)
class ActionErrorSpec:
    code: str
    status: int
    condition: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "condition": self.condition,
        }


@dataclass(frozen=True)
class ActionContractSpec:
    """Semantics OpenAPI cannot derive from a route signature."""

    classification: ActionClass
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    preconditions: tuple[str, ...] = ()
    state_effects: tuple[str, ...] = ()
    errors: tuple[ActionErrorSpec, ...] = ()
    audit_evidence: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "classification": self.classification,
            "output_schema": dict(self.output_schema),
            "preconditions": list(self.preconditions),
            "state_effects": list(self.state_effects),
            "errors": [item.to_dict() for item in self.errors],
            "audit_evidence": list(self.audit_evidence),
            "capability_tags": list(self.capability_tags),
        }


def action_contract(
    classification: ActionClass,
    *,
    output: Mapping[str, Any] | None = None,
    preconditions: tuple[str, ...] = (),
    effects: tuple[str, ...] = (),
    errors: tuple[tuple[str, int, str], ...] = (),
    audit: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> ActionContractSpec:
    """Compact service-local declaration helper."""
    return ActionContractSpec(
        classification=classification,
        output_schema=output or {},
        preconditions=preconditions,
        state_effects=effects,
        errors=tuple(
            ActionErrorSpec(code=code, status=status, condition=condition)
            for code, status, condition in errors
        ),
        audit_evidence=audit,
        capability_tags=tags,
    )


def validate_contract_declarations(
    *,
    live_actions: set[str],
    contracts: Mapping[str, ActionContractSpec],
    profiles: Mapping[str, tuple[str, ...]],
) -> list[str]:
    issues: list[str] = []
    contract_actions = set(contracts)
    missing = sorted(live_actions - contract_actions)
    extra = sorted(contract_actions - live_actions)
    if missing:
        issues.append(f"live actions lack contracts: {missing!r}")
    if extra:
        issues.append(f"contracts reference non-live actions: {extra!r}")
    for profile, actions in profiles.items():
        unknown = sorted(set(actions) - live_actions)
        if unknown:
            issues.append(
                f"profile {profile!r} references unknown actions {unknown!r}"
            )
        duplicates = sorted({
            action for action in actions if actions.count(action) > 1
        })
        if duplicates:
            issues.append(
                f"profile {profile!r} contains duplicates {duplicates!r}"
            )
        environment = sorted(
            action for action in actions
            if contracts.get(action)
            and contracts[action].classification == "environment_only"
        )
        if environment:
            issues.append(
                f"profile {profile!r} exposes environment actions {environment!r}"
            )
    return issues
