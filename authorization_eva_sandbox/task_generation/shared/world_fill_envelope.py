"""Deterministic Stage 4 Fill Envelope from gate-passed Stage 2/3 artifacts."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from authorization_eva_sandbox.task_generation.shared.artifact_gate import (
    content_sha256,
    registry_contract_snapshot,
)

try:
    from services.fixture_slots import (
        FixtureFieldSpec,
        FixtureSlotSpec,
        FixtureTemplateSpec,
        nested_collection_path,
        overlapping_slot_issues,
        path_root,
        runtime_fixture_template,
        slot_path_index,
    )
except ModuleNotFoundError:  # package-mode tests
    from authorization_eva_sandbox.services.fixture_slots import (
        FixtureFieldSpec,
        FixtureSlotSpec,
        FixtureTemplateSpec,
        nested_collection_path,
        overlapping_slot_issues,
        path_root,
        runtime_fixture_template,
        slot_path_index,
    )


SCHEMA_VERSION = 1
DEFAULT_EXPOSURE_PROFILE = "full-v1"
ALWAYS_MOUNTED_TEMPLATES = ("user_messaging",)
INSTRUCTION_KIND = "instruction"

RUNTIME_MECHANISM_IDS = {
    "workspace_files": "workspace_files",
    "prior_memory": "prior_memory",
    "runtime:shell": "shell",
    "runtime:user_chat": "user_chat",
}
REJECTED_CARRIER_ALIASES = {
    "runtime:workspace_files",
    "runtime:prior_memory",
    "injected:context",
}
CLOSED_RUNTIME_ACTIONS: dict[str, dict[str, Any]] = {
    "injected:context": {
        "action_id": "injected:context",
        "template": "runtime:user_chat",
        "classification": "environment_only",
        "agent_exposed": False,
        "signature": "injected:context",
        "contract": {
            "classification": "environment_only",
            "output_schema": {},
            "preconditions": [],
            "state_effects": ["deliver the immutable Stage 1 instruction"],
            "errors": [],
            "audit_evidence": [],
            "capability_tags": ["instruction", "context"],
        },
    },
    "runtime:user_chat.message_user": {
        "action_id": "runtime:user_chat.message_user",
        "template": "runtime:user_chat",
        "classification": "runtime",
        "agent_exposed": True,
        "signature": "message_user(message: string)",
        "contract": {
            "classification": "runtime",
            "output_schema": {"delivered": "boolean"},
            "preconditions": [],
            "state_effects": ["send one message to the principal"],
            "errors": [],
            "audit_evidence": ["action", "status"],
            "capability_tags": ["message", "send", "user"],
        },
    },
    "runtime:shell.read_file": {
        "action_id": "runtime:shell.read_file",
        "template": "runtime:shell",
        "classification": "runtime",
        "agent_exposed": True,
        "signature": "runtime:shell.read_file(path)",
        "contract": {
            "classification": "runtime",
            "output_schema": {"content": "string"},
            "preconditions": [],
            "state_effects": [],
            "errors": [],
            "audit_evidence": ["action", "path"],
            "capability_tags": ["read", "file", "workspace"],
        },
    },
    "runtime:shell.write_file": {
        "action_id": "runtime:shell.write_file",
        "template": "runtime:shell",
        "classification": "runtime",
        "agent_exposed": True,
        "signature": "runtime:shell.write_file(path, content)",
        "contract": {
            "classification": "runtime",
            "output_schema": {"ok": "boolean"},
            "preconditions": [],
            "state_effects": ["write one workspace file"],
            "errors": [],
            "audit_evidence": ["action", "path"],
            "capability_tags": ["write", "file", "workspace"],
        },
    },
    "runtime:shell.bash": {
        "action_id": "runtime:shell.bash",
        "template": "runtime:shell",
        "classification": "runtime",
        "agent_exposed": True,
        "signature": "runtime:shell.bash(command)",
        "contract": {
            "classification": "runtime",
            "output_schema": {"exit_code": "integer"},
            "preconditions": [],
            "state_effects": ["run one sandbox command"],
            "errors": [],
            "audit_evidence": ["action", "command"],
            "capability_tags": ["shell", "command", "bash"],
        },
    },
}


class FillEnvelopeError(ValueError):
    """Deterministic Fill Envelope construction or validation failure."""


@dataclass(frozen=True)
class FieldOwnership:
    required_model_fields: tuple[str, ...] = ()
    optional_model_fields: tuple[str, ...] = ()
    fixed_fields: tuple[str, ...] = ()
    forbidden_model_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_model_fields": list(self.required_model_fields),
            "optional_model_fields": list(self.optional_model_fields),
            "fixed_fields": list(self.fixed_fields),
            "forbidden_model_fields": list(self.forbidden_model_fields),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FieldOwnership":
        return cls(
            required_model_fields=tuple(raw.get("required_model_fields") or ()),
            optional_model_fields=tuple(raw.get("optional_model_fields") or ()),
            fixed_fields=tuple(raw.get("fixed_fields") or ()),
            forbidden_model_fields=tuple(
                raw.get("forbidden_model_fields") or ()
            ),
        )


@dataclass(frozen=True)
class EnvelopeScope:
    path: str
    slot_ids: tuple[str, ...]
    material_ids: tuple[str, ...]
    ownership: FieldOwnership
    cardinality: str
    allowed_collection_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_ids": list(self.slot_ids),
            "material_ids": list(self.material_ids),
            "required_model_fields": list(self.ownership.required_model_fields),
            "optional_model_fields": list(self.ownership.optional_model_fields),
            "fixed_fields": list(self.ownership.fixed_fields),
            "forbidden_model_fields": list(
                self.ownership.forbidden_model_fields
            ),
            "cardinality": self.cardinality,
            "allowed_collection_paths": list(self.allowed_collection_paths),
        }

    @classmethod
    def from_dict(cls, path: str, raw: Mapping[str, Any]) -> "EnvelopeScope":
        return cls(
            path=path,
            slot_ids=tuple(raw.get("slot_ids") or ()),
            material_ids=tuple(raw.get("material_ids") or ()),
            ownership=FieldOwnership.from_dict(raw),
            cardinality=str(raw.get("cardinality") or ""),
            allowed_collection_paths=tuple(
                raw.get("allowed_collection_paths") or ()
            ),
        )


@dataclass(frozen=True)
class EnvelopeTarget:
    target_id: str
    kind: str
    object_id: str
    template: str
    fixture_document: str
    scopes: dict[str, EnvelopeScope]
    send_to_model: bool
    action_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object_id": self.object_id,
            "template": self.template,
            "fixture_document": self.fixture_document,
            "scopes": {
                path: scope.to_dict() for path, scope in self.scopes.items()
            },
            "send_to_model": self.send_to_model,
            "action_only": self.action_only,
        }

    @classmethod
    def from_dict(cls, target_id: str, raw: Mapping[str, Any]) -> "EnvelopeTarget":
        scopes = {
            str(path): EnvelopeScope.from_dict(str(path), value)
            for path, value in (raw.get("scopes") or {}).items()
        }
        return cls(
            target_id=target_id,
            kind=str(raw.get("kind") or ""),
            object_id=str(raw.get("object_id") or ""),
            template=str(raw.get("template") or ""),
            fixture_document=str(raw.get("fixture_document") or ""),
            scopes=scopes,
            send_to_model=bool(raw.get("send_to_model")),
            action_only=bool(raw.get("action_only")),
        )


@dataclass(frozen=True)
class EnvelopeMaterial:
    material_id: str
    kind: str
    target_id: str
    slot_id: str
    allowed_roots: tuple[str, ...]
    content_conditions: tuple[str, ...]
    related_steps: tuple[dict[str, str], ...]
    instruction_verbatim: str | None = None
    coverage_without_model: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = {
            "kind": self.kind,
            "target_id": self.target_id,
            "slot_id": self.slot_id,
            "allowed_roots": list(self.allowed_roots),
            "content_conditions": list(self.content_conditions),
            "related_steps": [dict(item) for item in self.related_steps],
            "coverage_without_model": self.coverage_without_model,
        }
        if self.instruction_verbatim is not None:
            result["instruction_verbatim"] = self.instruction_verbatim
        return result

    @classmethod
    def from_dict(
        cls, material_id: str, raw: Mapping[str, Any]
    ) -> "EnvelopeMaterial":
        return cls(
            material_id=material_id,
            kind=str(raw.get("kind") or ""),
            target_id=str(raw.get("target_id") or ""),
            slot_id=str(raw.get("slot_id") or ""),
            allowed_roots=tuple(raw.get("allowed_roots") or ()),
            content_conditions=tuple(raw.get("content_conditions") or ()),
            related_steps=tuple(
                dict(item) for item in (raw.get("related_steps") or [])
            ),
            instruction_verbatim=raw.get("instruction_verbatim"),
            coverage_without_model=bool(raw.get("coverage_without_model")),
        )


@dataclass(frozen=True)
class EnvelopeAction:
    action_id: str
    template: str
    classification: str
    agent_exposed: bool
    signature: str
    contract: dict[str, Any]
    step_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "classification": self.classification,
            "agent_exposed": self.agent_exposed,
            "signature": self.signature,
            "contract": copy.deepcopy(self.contract),
            "step_ids": list(self.step_ids),
        }

    @classmethod
    def from_dict(cls, action_id: str, raw: Mapping[str, Any]) -> "EnvelopeAction":
        return cls(
            action_id=action_id,
            template=str(raw.get("template") or ""),
            classification=str(raw.get("classification") or ""),
            agent_exposed=bool(raw.get("agent_exposed")),
            signature=str(raw.get("signature") or ""),
            contract=copy.deepcopy(raw.get("contract") or {}),
            step_ids=tuple(raw.get("step_ids") or ()),
        )


@dataclass(frozen=True)
class FillEnvelope:
    schema_version: int
    scenario_id: str
    instruction: str
    exposure_profile: str
    upstream_hashes: dict[str, str]
    registry_snapshot_sha256: str
    targets: dict[str, EnvelopeTarget]
    materials: dict[str, EnvelopeMaterial]
    actions: dict[str, EnvelopeAction]
    environment_actions: dict[str, EnvelopeAction]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "instruction": self.instruction,
            "exposure_profile": self.exposure_profile,
            "upstream_hashes": dict(self.upstream_hashes),
            "registry_snapshot_sha256": self.registry_snapshot_sha256,
            "targets": {
                target_id: target.to_dict()
                for target_id, target in self.targets.items()
            },
            "materials": {
                material_id: material.to_dict()
                for material_id, material in self.materials.items()
            },
            "actions": {
                action_id: action.to_dict()
                for action_id, action in self.actions.items()
            },
            "environment_actions": {
                action_id: action.to_dict()
                for action_id, action in self.environment_actions.items()
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FillEnvelope":
        if not isinstance(raw, Mapping):
            raise FillEnvelopeError("envelope must be an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise FillEnvelopeError(
                f"unsupported envelope schema_version {raw.get('schema_version')!r}"
            )
        return cls(
            schema_version=SCHEMA_VERSION,
            scenario_id=str(raw.get("scenario_id") or ""),
            instruction=str(raw.get("instruction") or ""),
            exposure_profile=str(
                raw.get("exposure_profile") or DEFAULT_EXPOSURE_PROFILE
            ),
            upstream_hashes={
                str(key): str(value)
                for key, value in (raw.get("upstream_hashes") or {}).items()
            },
            registry_snapshot_sha256=str(
                raw.get("registry_snapshot_sha256") or ""
            ),
            targets={
                str(target_id): EnvelopeTarget.from_dict(str(target_id), value)
                for target_id, value in (raw.get("targets") or {}).items()
            },
            materials={
                str(material_id): EnvelopeMaterial.from_dict(
                    str(material_id), value
                )
                for material_id, value in (raw.get("materials") or {}).items()
            },
            actions={
                str(action_id): EnvelopeAction.from_dict(str(action_id), value)
                for action_id, value in (raw.get("actions") or {}).items()
            },
            environment_actions={
                str(action_id): EnvelopeAction.from_dict(str(action_id), value)
                for action_id, value in (
                    raw.get("environment_actions") or {}
                ).items()
            },
        )

    def model_target_ids(self) -> tuple[str, ...]:
        return tuple(
            target_id for target_id, target in self.targets.items()
            if target.send_to_model
        )

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def unwrap_blueprint(document: Mapping[str, Any]) -> dict[str, Any]:
    if "assembly_blueprint" in document:
        blueprint = document["assembly_blueprint"]
        if not isinstance(blueprint, dict):
            raise FillEnvelopeError("assembly_blueprint must be an object")
        return blueprint
    return dict(document)


def unwrap_script(document: Mapping[str, Any]) -> dict[str, Any]:
    if "step2_script" in document:
        script = document["step2_script"]
        if not isinstance(script, dict):
            raise FillEnvelopeError("step2_script must be an object")
        return script
    return dict(document)


def physical_target_id(object_id: str, template_or_mechanism: str) -> str:
    if template_or_mechanism in REJECTED_CARRIER_ALIASES:
        raise FillEnvelopeError(
            f"rejected legacy carrier alias {template_or_mechanism!r}"
        )
    mechanism = RUNTIME_MECHANISM_IDS.get(template_or_mechanism)
    if mechanism is not None:
        return f"runtime:{object_id}:{mechanism}"
    if not object_id or not template_or_mechanism:
        raise FillEnvelopeError("target identity requires object_id and template")
    if ":" in template_or_mechanism or "/" in template_or_mechanism:
        raise FillEnvelopeError(
            f"unknown template or mechanism {template_or_mechanism!r}"
        )
    return f"service:{object_id}:{template_or_mechanism}"


def _related_steps(script: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    related: dict[str, list[dict[str, str]]] = {}
    for section in ("common_setup", "authorized_path", "violating_path"):
        for step in script.get(section) or []:
            if not isinstance(step, dict):
                continue
            for material_id in step.get("uses_materials") or []:
                related.setdefault(str(material_id), []).append({
                    "id": str(step.get("id") or ""),
                    "path": section,
                    "action_intent": str(step.get("action_intent") or ""),
                    "effect": str(step.get("effect") or ""),
                    "narrative_evidence": str(
                        step.get("narrative_evidence") or ""
                    ),
                })
    return related


def _selected_templates(blueprint: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    owners: dict[str, list[str]] = {}
    for carrier in blueprint.get("carriers") or []:
        object_id = str(carrier.get("object_id") or "")
        for template in carrier.get("templates") or []:
            name = str(template)
            names.append(name)
            owners.setdefault(name, []).append(object_id)
    duplicates = {
        name: object_ids for name, object_ids in owners.items()
        if len(object_ids) > 1
    }
    if duplicates:
        raise FillEnvelopeError(
            "duplicate instances of the same template are rejected until "
            "actions carry an explicit instance target: "
            + ", ".join(
                f"{name} on {object_ids!r}"
                for name, object_ids in sorted(duplicates.items())
            )
        )
    return tuple(dict.fromkeys(names))


def _template_spec(registry: Any, template: str) -> Any:
    spec = registry.templates.get(template)
    if spec is None:
        raise FillEnvelopeError(f"unknown template {template!r}")
    return spec


def _fixture_template(
    registry: Any, template_or_mechanism: str
) -> FixtureTemplateSpec:
    if template_or_mechanism in RUNTIME_MECHANISM_IDS:
        runtime = runtime_fixture_template(template_or_mechanism)
        if runtime is None:
            raise FillEnvelopeError(
                f"missing runtime fixture template {template_or_mechanism!r}"
            )
        return runtime
    spec = _template_spec(registry, template_or_mechanism)
    if not spec.converted:
        raise FillEnvelopeError(f"template {template_or_mechanism!r} is not converted")
    if spec.fixture_template is None:
        raise FillEnvelopeError(
            f"template {template_or_mechanism!r} declares no fixture template"
        )
    return spec.fixture_template


def _resolve_slot(
    template: FixtureTemplateSpec, slot_id: str
) -> FixtureSlotSpec:
    slot = next(
        (item for item in template.slots if item.slot_id == slot_id),
        None,
    )
    if slot is None:
        raise FillEnvelopeError(f"unknown fixture slot {slot_id!r}")
    return slot


def partition_field_ownership(
    template: FixtureTemplateSpec,
    roots: Iterable[str],
) -> FieldOwnership:
    active = set(roots)
    required: list[str] = []
    optional: list[str] = []
    fixed: list[str] = []
    for field_spec in template.fields:
        if path_root(field_spec.path) not in active:
            continue
        if field_spec.source == "model":
            if field_spec.default is None:
                required.append(field_spec.path)
            else:
                optional.append(field_spec.path)
        else:
            fixed.append(field_spec.path)
    return FieldOwnership(
        required_model_fields=tuple(required),
        optional_model_fields=tuple(optional),
        fixed_fields=tuple(fixed),
        forbidden_model_fields=tuple(fixed),
    )


def _scope_cardinality(schema: dict[str, Any], path: str) -> str:
    branch = schema.get(path)
    if isinstance(branch, list):
        return "collection"
    if isinstance(branch, dict):
        return "singleton"
    return "scalar"


def _collection_paths_for_root(
    template: FixtureTemplateSpec, root: str
) -> tuple[str, ...]:
    paths: list[str] = []
    for field_spec in template.fields:
        collection = nested_collection_path(field_spec.path)
        if collection and path_root(field_spec.path) == root:
            paths.append(collection)
    return tuple(dict.fromkeys(paths))


def _flatten_blueprint_actions(
    blueprint: Mapping[str, Any],
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for entry in blueprint.get("actions") or []:
        if not isinstance(entry, dict):
            raise FillEnvelopeError("blueprint action must be an object")
        step_id = str(entry.get("step_id") or "")
        raw = entry.get("action")
        selected = raw if isinstance(raw, list) else [raw]
        if not selected:
            raise FillEnvelopeError(f"step {step_id!r} has no action")
        for action_id in selected:
            pairs.append((step_id, str(action_id)))
    return pairs


def _exposure_actions(spec: Any, profile: str) -> set[str]:
    profiles = spec.capability_profiles or {}
    if profile in profiles:
        return set(profiles[profile])
    if DEFAULT_EXPOSURE_PROFILE in profiles:
        return set(profiles[DEFAULT_EXPOSURE_PROFILE])
    if "standard-v1" in profiles:
        return set(profiles["standard-v1"])
    return {action.name for action in spec.actions}


def _match_live_action(
    action_id: str,
    *,
    registry: Any,
    selected_templates: Iterable[str],
) -> tuple[str, Any] | None:
    matches: list[tuple[str, Any]] = []
    lookup_templates = list(dict.fromkeys(
        [*selected_templates, *ALWAYS_MOUNTED_TEMPLATES]
    ))
    for template in lookup_templates:
        prefix = f"{template}."
        if not action_id.startswith(prefix):
            continue
        name = action_id[len(prefix):]
        resolved = registry.resolve_action(template, name)
        if resolved is not None:
            matches.append((template, resolved))
    if len(matches) > 1:
        raise FillEnvelopeError(
            f"action {action_id!r} matches multiple templates "
            f"{[item[0] for item in matches]!r}"
        )
    return matches[0] if matches else None


def resolve_blueprint_action(
    action_id: str,
    *,
    registry: Any,
    selected_templates: Iterable[str],
    exposure_profile: str,
) -> EnvelopeAction:
    closed = CLOSED_RUNTIME_ACTIONS.get(action_id)
    if closed is not None:
        return EnvelopeAction(
            action_id=action_id,
            template=str(closed["template"]),
            classification=str(closed["classification"]),
            agent_exposed=bool(closed["agent_exposed"]),
            signature=str(closed["signature"]),
            contract=copy.deepcopy(closed["contract"]),
        )
    matched = _match_live_action(
        action_id, registry=registry, selected_templates=selected_templates
    )
    if matched is None:
        raise FillEnvelopeError(
            f"action {action_id!r} is not an exact live or closed runtime action"
        )
    template, spec = matched
    template_spec = registry.templates[template]
    contract = spec.contract
    classification = (
        contract.classification if contract is not None else "legacy"
    )
    if classification == "environment_only":
        raise FillEnvelopeError(
            f"environment-only action {action_id!r} cannot be on the agent "
            "tool surface"
        )
    if template not in ALWAYS_MOUNTED_TEMPLATES:
        allowed = _exposure_actions(template_spec, exposure_profile)
        if spec.name not in allowed:
            raise FillEnvelopeError(
                f"action {action_id!r} is not in exposure profile "
                f"{exposure_profile!r}"
            )
        if contract is None:
            raise FillEnvelopeError(
                f"action {action_id!r} has no Action Contract"
            )
    return EnvelopeAction(
        action_id=action_id,
        template=template,
        classification=str(classification),
        agent_exposed=True,
        signature=spec.signature(),
        contract=(
            contract.to_dict() if contract is not None else {
                "classification": "legacy",
                "output_schema": {},
                "preconditions": [],
                "state_effects": [],
                "errors": [],
                "audit_evidence": [],
                "capability_tags": [],
            }
        ),
    )


def _environment_action_catalog(
    registry: Any, selected_templates: Iterable[str]
) -> dict[str, EnvelopeAction]:
    catalog: dict[str, EnvelopeAction] = {}
    for template in selected_templates:
        spec = registry.templates.get(template)
        if spec is None:
            continue
        for name, contract in (spec.environment_actions or {}).items():
            action_id = f"{template}.{name}"
            catalog[action_id] = EnvelopeAction(
                action_id=action_id,
                template=template,
                classification="environment_only",
                agent_exposed=False,
                signature=f"{action_id}()",
                contract=contract.to_dict(),
            )
    catalog["injected:context"] = EnvelopeAction(
        action_id="injected:context",
        template="runtime:user_chat",
        classification="environment_only",
        agent_exposed=False,
        signature="injected:context",
        contract=copy.deepcopy(
            CLOSED_RUNTIME_ACTIONS["injected:context"]["contract"]
        ),
    )
    return catalog


def _send_to_model(target: EnvelopeTarget, materials: list[EnvelopeMaterial]) -> bool:
    if target.action_only:
        return False
    if materials and all(item.coverage_without_model for item in materials):
        return False
    if materials and any(not item.coverage_without_model for item in materials):
        return True
    has_model_fields = any(
        scope.ownership.required_model_fields
        or scope.ownership.optional_model_fields
        for scope in target.scopes.values()
    )
    return has_model_fields


def build_fill_envelope(
    *,
    scenario_id: str,
    policy_skeleton: Mapping[str, Any],
    script: Mapping[str, Any],
    blueprint: Mapping[str, Any],
    registry: Any,
    upstream_hashes: Mapping[str, str] | None = None,
    exposure_profile: str = DEFAULT_EXPOSURE_PROFILE,
    registry_snapshot: Mapping[str, Any] | None = None,
) -> FillEnvelope:
    blueprint = unwrap_blueprint(blueprint)
    script = unwrap_script(script)
    if blueprint.get("unresolved"):
        raise FillEnvelopeError("blueprint.unresolved must be empty")

    selected = _selected_templates(blueprint)
    for template in selected:
        _template_spec(registry, template)

    snapshot = registry_snapshot or registry_contract_snapshot()
    instruction = str(policy_skeleton.get("instruction") or "")
    if not instruction:
        raise FillEnvelopeError("Stage 1 instruction is missing")

    related = _related_steps(script)
    materials_by_id = {
        str(item.get("id")): item
        for item in script.get("materials") or []
        if isinstance(item, dict)
    }
    if not materials_by_id:
        raise FillEnvelopeError("script has no materials")

    bindings: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    placed: set[str] = set()
    for carrier in blueprint.get("carriers") or []:
        object_id = str(carrier.get("object_id") or "")
        if not object_id:
            raise FillEnvelopeError("carrier is missing object_id")
        for binding in carrier.get("material_bindings") or []:
            if not isinstance(binding, dict):
                raise FillEnvelopeError("material binding must be an object")
            material_id = str(binding.get("material_id") or "")
            if material_id in placed:
                raise FillEnvelopeError(
                    f"material {material_id!r} is bound more than once"
                )
            placed.add(material_id)
            bindings.append((object_id, carrier, binding))
    missing = sorted(set(materials_by_id) - placed)
    if missing:
        raise FillEnvelopeError(f"materials have no carrier binding: {missing!r}")
    extra = sorted(placed - set(materials_by_id))
    if extra:
        raise FillEnvelopeError(f"bindings reference unknown materials: {extra!r}")

    target_slots: dict[str, dict[str, Any]] = {}
    envelope_materials: dict[str, EnvelopeMaterial] = {}
    for object_id, _carrier, binding in bindings:
        material_id = str(binding["material_id"])
        material = materials_by_id[material_id]
        raw_target = str(binding.get("template_or_mechanism") or "")
        slot_id = str(binding.get("fixture_slot") or "")
        if not slot_id:
            raise FillEnvelopeError(f"material {material_id!r} has no fixture_slot")
        target_id = physical_target_id(object_id, raw_target)
        fixture = _fixture_template(registry, raw_target)
        slot = _resolve_slot(fixture, slot_id)
        kind = str(material.get("kind") or "")
        overlap = overlapping_slot_issues(fixture.slots, fixture.schema)
        if overlap:
            raise FillEnvelopeError(
                f"{raw_target} overlapping slot contracts: " + "; ".join(overlap)
            )
        entry = target_slots.setdefault(target_id, {
            "kind": "runtime" if target_id.startswith("runtime:") else "service",
            "object_id": object_id,
            "template": raw_target,
            "fixture": fixture,
            "slot_ids": [],
            "bindings": [],
        })
        if entry["template"] != raw_target or entry["object_id"] != object_id:
            raise FillEnvelopeError(
                f"target {target_id!r} mixed identity {entry['template']!r}/"
                f"{raw_target!r}"
            )
        entry["slot_ids"].append(slot_id)
        entry["bindings"].append((material_id, slot))
        instruction_copy = (
            instruction if kind == INSTRUCTION_KIND else None
        )
        envelope_materials[material_id] = EnvelopeMaterial(
            material_id=material_id,
            kind=kind,
            target_id=target_id,
            slot_id=slot_id,
            allowed_roots=tuple(slot.paths),
            content_conditions=tuple(material.get("content_conditions") or ()),
            related_steps=tuple(related.get(material_id, [])),
            instruction_verbatim=instruction_copy,
            coverage_without_model=kind == INSTRUCTION_KIND,
        )

    for carrier in blueprint.get("carriers") or []:
        object_id = str(carrier.get("object_id") or "")
        bound_templates = {
            str(binding.get("template_or_mechanism") or "")
            for binding in carrier.get("material_bindings") or []
        }
        for template in carrier.get("templates") or []:
            name = str(template)
            if name in bound_templates:
                continue
            target_id = physical_target_id(object_id, name)
            target_slots.setdefault(target_id, {
                "kind": "service",
                "object_id": object_id,
                "template": name,
                "fixture": _fixture_template(registry, name),
                "slot_ids": [],
                "bindings": [],
                "action_only": True,
            })

    action_steps: dict[str, list[str]] = {}
    actions: dict[str, EnvelopeAction] = {}
    environment_actions = _environment_action_catalog(registry, selected)
    for step_id, action_id in _flatten_blueprint_actions(blueprint):
        action_steps.setdefault(action_id, []).append(step_id)
        if action_id in environment_actions and action_id not in CLOSED_RUNTIME_ACTIONS:
            raise FillEnvelopeError(
                f"environment-only action {action_id!r} cannot be on the agent "
                "tool surface"
            )
        if action_id == "injected:context":
            actions[action_id] = environment_actions[action_id]
            continue
        actions[action_id] = resolve_blueprint_action(
            action_id,
            registry=registry,
            selected_templates=selected,
            exposure_profile=exposure_profile,
        )
    for action_id, action in list(actions.items()):
        actions[action_id] = EnvelopeAction(
            action_id=action.action_id,
            template=action.template,
            classification=action.classification,
            agent_exposed=action.agent_exposed,
            signature=action.signature,
            contract=action.contract,
            step_ids=tuple(action_steps.get(action_id, ())),
        )
        if action.classification == "environment_only":
            environment_actions[action_id] = actions[action_id]
            if not action.agent_exposed:
                actions.pop(action_id, None)

    for action in actions.values():
        if (
            action.template not in RUNTIME_MECHANISM_IDS
            and action.template not in selected
            and action.template not in ALWAYS_MOUNTED_TEMPLATES
            and not action.template.startswith("runtime:")
        ):
            raise FillEnvelopeError(
                f"action {action.action_id!r} uses unselected template "
                f"{action.template!r}"
            )

    targets: dict[str, EnvelopeTarget] = {}
    for target_id, entry in target_slots.items():
        fixture: FixtureTemplateSpec = entry["fixture"]
        path_owners = slot_path_index(
            _resolve_slot(fixture, slot_id)
            for slot_id in dict.fromkeys(entry["slot_ids"])
        ) if entry["slot_ids"] else {}
        materials_by_path: dict[str, list[str]] = {}
        for material_id, slot in entry["bindings"]:
            for path in slot.paths:
                materials_by_path.setdefault(path, []).append(material_id)
        scopes: dict[str, EnvelopeScope] = {}
        for path, owners in path_owners.items():
            ownership = partition_field_ownership(fixture, [path])
            scopes[path] = EnvelopeScope(
                path=path,
                slot_ids=tuple(dict.fromkeys(slot.slot_id for slot in owners)),
                material_ids=tuple(dict.fromkeys(materials_by_path.get(path, []))),
                ownership=ownership,
                cardinality=_scope_cardinality(fixture.schema, path),
                allowed_collection_paths=_collection_paths_for_root(
                    fixture, path
                ),
            )
        document = (
            RUNTIME_MECHANISM_IDS.get(entry["template"], entry["template"])
            if entry["kind"] == "runtime"
            else entry["template"]
        )
        target = EnvelopeTarget(
            target_id=target_id,
            kind=entry["kind"],
            object_id=entry["object_id"],
            template=entry["template"],
            fixture_document=document,
            scopes=scopes,
            send_to_model=False,
            action_only=bool(entry.get("action_only")),
        )
        owned = [
            envelope_materials[material_id]
            for material_id, _ in entry["bindings"]
        ]
        targets[target_id] = EnvelopeTarget(
            target_id=target.target_id,
            kind=target.kind,
            object_id=target.object_id,
            template=target.template,
            fixture_document=target.fixture_document,
            scopes=target.scopes,
            send_to_model=_send_to_model(target, owned),
            action_only=target.action_only,
        )

    envelope = FillEnvelope(
        schema_version=SCHEMA_VERSION,
        scenario_id=scenario_id,
        instruction=instruction,
        exposure_profile=exposure_profile,
        upstream_hashes=dict(upstream_hashes or {}),
        registry_snapshot_sha256=content_sha256(snapshot),
        targets=targets,
        materials=envelope_materials,
        actions=actions,
        environment_actions=environment_actions,
    )
    issues = validate_fill_envelope(envelope, registry=registry)
    if issues:
        raise FillEnvelopeError("; ".join(issues))
    return envelope


def validate_fill_envelope(
    envelope: FillEnvelope,
    *,
    registry: Any | None = None,
) -> list[str]:
    issues: list[str] = []
    if envelope.schema_version != SCHEMA_VERSION:
        issues.append(
            f"unsupported schema_version {envelope.schema_version!r}"
        )
    if not envelope.scenario_id:
        issues.append("scenario_id is empty")
    if not envelope.instruction:
        issues.append("instruction is empty")
    seen_records: set[tuple[str, str]] = set()
    for material_id, material in envelope.materials.items():
        target = envelope.targets.get(material.target_id)
        if target is None:
            issues.append(
                f"material {material_id!r} references missing target "
                f"{material.target_id!r}"
            )
            continue
        if material.slot_id not in {
            slot_id for scope in target.scopes.values()
            for slot_id in scope.slot_ids
        } and not target.action_only:
            issues.append(
                f"material {material_id!r} slot {material.slot_id!r} is not "
                f"on target {material.target_id!r}"
            )
        if material.kind == INSTRUCTION_KIND:
            if material.instruction_verbatim != envelope.instruction:
                issues.append(
                    f"instruction material {material_id!r} is not the exact "
                    "Stage 1 instruction"
                )
            if not material.coverage_without_model:
                issues.append(
                    f"instruction material {material_id!r} must be marked "
                    "coverage_without_model"
                )
        key = (material.target_id, material.slot_id)
        seen_records.add(key)
    if registry is not None:
        try:
            current = content_sha256(registry_contract_snapshot())
        except Exception as exc:  # pragma: no cover - registry import failures
            issues.append(f"live registry snapshot failed: {exc}")
        else:
            if (
                envelope.registry_snapshot_sha256
                and envelope.registry_snapshot_sha256 != current
            ):
                issues.append("registry contract snapshot drifted")
    for target_id, target in envelope.targets.items():
        extra = set(target.scopes) - {
            root for material in envelope.materials.values()
            if material.target_id == target_id
            for root in material.allowed_roots
        }
        if extra and not target.action_only:
            issues.append(
                f"target {target_id!r} has unused constructed scopes {sorted(extra)!r}"
            )
    return issues


def stage3_registry_sha(blueprint: Mapping[str, Any]) -> str | None:
    receipt = blueprint.get("_gate")
    if not isinstance(receipt, Mapping):
        return None
    inputs = receipt.get("inputs")
    if not isinstance(inputs, Mapping):
        return None
    digest = inputs.get("registry")
    return str(digest) if digest else None


def require_stage3_registry_match(blueprint: Mapping[str, Any]) -> str:
    """Stop Stage 4 when the live registry no longer matches the Stage 3 receipt."""
    stored = stage3_registry_sha(blueprint)
    if stored is None:
        raise FillEnvelopeError("Stage 3 receipt is missing registry SHA")
    current = content_sha256(registry_contract_snapshot())
    if stored != current:
        raise FillEnvelopeError(
            f"live registry SHA {current} does not match Stage 3 receipt {stored}"
        )
    return current
