"""Machine-readable materialization slots derived from FIXTURE_SCHEMA.

Schemas own field structure. Slots only describe which schema branches form a
semantic material carrier and which fields are compiler-owned.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
from typing import Any, Iterable


FIELD_SOURCES = {
    "model", "compiler", "timeplan", "shared_context", "private_runtime"
}
FIELD_FORMAT_TYPES = {
    "person_name": {"str"},
    "email_address": {"str"},
    "email_identity": {"str"},
    "email_addresses": {"str"},
    "plain_text": {"str"},
    "markdown": {"str"},
    "iso8601": {"str"},
    "date": {"str"},
    "time_of_day": {"str"},
    "stable_id": {"str"},
    "integer_percent": {"int"},
    "positive_integer": {"int"},
    "duration_minutes": {"int"},
    "boolean": {"bool"},
    "enum": {"enum"},
    "email_list": {"list"},
    "filename_list": {"list"},
    "string_list": {"list"},
    "datetime_windows": {"list"},
    "json_list": {"list"},
    "absolute_path": {"str"},
    "relative_path": {"str"},
}


@dataclass(frozen=True)
class FixtureFieldSpec:
    path: str
    source: str
    meaning: str
    format: str = ""
    default: Any = None

    @classmethod
    def from_dict(cls, path: str, raw: dict[str, Any]) -> "FixtureFieldSpec":
        return cls(
            path=path,
            source=str(raw.get("source") or ""),
            meaning=str(raw.get("meaning") or ""),
            format=str(raw.get("format") or ""),
            default=copy.deepcopy(raw.get("default")),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "source": self.source,
            "meaning": self.meaning,
            "format": self.format,
        }
        if self.default is not None:
            result["default"] = copy.deepcopy(self.default)
        return result


@dataclass(frozen=True)
class FixtureSlotSpec:
    slot_id: str
    paths: tuple[str, ...]
    material_kinds: tuple[str, ...] = ("*",)
    description: str = ""
    compiler_fields: tuple[str, ...] = ()
    cardinality: str = "scenario"
    bulk_generator: str = ""

    def accepts(self, material_kind: str) -> bool:
        return "*" in self.material_kinds or material_kind in self.material_kinds

    def to_dict(self, schema: dict[str, Any]) -> dict[str, Any]:
        content_schema = {
            path: copy.deepcopy(schema[path])
            for path in self.paths if path in schema
        }
        for compiler_field in self.compiler_fields:
            _remove_schema_field(content_schema, compiler_field.split("."))
        return {
            "slot_id": self.slot_id,
            "paths": list(self.paths),
            "material_kinds": list(self.material_kinds),
            "description": self.description,
            "compiler_fields": list(self.compiler_fields),
            "cardinality": self.cardinality,
            "bulk_generator": self.bulk_generator,
            "content_schema": content_schema,
        }


@dataclass(frozen=True)
class FixtureTemplateSpec:
    """One service's complete, pre-registered fixture contract."""

    schema: dict[str, Any]
    fields: tuple[FixtureFieldSpec, ...]
    slots: tuple[FixtureSlotSpec, ...]
    migrated: bool = False

    def field(self, path: str) -> FixtureFieldSpec | None:
        return next((field for field in self.fields if field.path == path), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": copy.deepcopy(self.schema),
            "fields": {
                field.path: field.to_dict() for field in self.fields
            },
            "materialization_slots": {
                slot.slot_id: slot.to_dict(self.schema) for slot in self.slots
            },
        }

    def generation_contract(self, slot: FixtureSlotSpec) -> dict[str, Any]:
        """Model-owned value schema and guidance for one preselected slot."""
        value_schema = {
            path: copy.deepcopy(self.schema[path])
            for path in slot.paths
        }
        fields = {
            field.path: field for field in self.fields
            if field.path.split(".", 1)[0].removesuffix("[]") in slot.paths
        }
        for field in fields.values():
            if field.source != "model":
                _remove_schema_field(value_schema, field.path.split("."))
        return {
            "slot_id": slot.slot_id,
            "cardinality": slot.cardinality,
            "bulk_generator": slot.bulk_generator,
            "value_schema": value_schema,
            "model_fields": {
                path: field.to_dict()
                for path, field in fields.items()
                if field.source == "model"
            },
            "fixed_fields": {
                path: field.to_dict()
                for path, field in fields.items()
                if field.source != "model"
            },
        }


def _remove_schema_field(node: Any, tokens: list[str]) -> None:
    if not tokens:
        return
    token, rest = tokens[0], tokens[1:]
    if token.endswith("[]"):
        key = token[:-2]
        branch = node.get(key) if isinstance(node, dict) else None
        if isinstance(branch, list) and branch:
            _remove_schema_field(branch[0], rest)
        return
    if not isinstance(node, dict) or token not in node:
        return
    if rest:
        _remove_schema_field(node[token], rest)
    else:
        node.pop(token, None)


def _normalise_override(slot_id: str, raw: dict[str, Any]) -> FixtureSlotSpec:
    paths = raw.get("paths") or [raw.get("path")]
    return FixtureSlotSpec(
        slot_id=slot_id,
        paths=tuple(str(p) for p in paths if p),
        material_kinds=tuple(str(k) for k in (
            raw.get("material_kinds") or ["*"]
        )),
        description=str(raw.get("description") or ""),
        compiler_fields=tuple(str(f) for f in (
            raw.get("compiler_fields") or []
        )),
        cardinality=str(raw.get("cardinality") or "scenario"),
        bulk_generator=str(raw.get("bulk_generator") or ""),
    )


def _auto_material_kinds(path: str) -> tuple[str, ...]:
    kinds = [path]
    if path.endswith("ies") and len(path) > 3:
        kinds.append(path[:-3] + "y")
    elif path.endswith("s") and len(path) > 1:
        kinds.append(path[:-1])
    return tuple(dict.fromkeys(kinds))


def _schema_path_exists(schema: Any, path: str) -> bool:
    node = schema
    for token in path.split("."):
        collection = token.endswith("[]")
        key = token[:-2] if collection else token
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
        if collection:
            if not isinstance(node, list) or len(node) != 1:
                return False
            node = node[0]
    return True


def _schema_at_path(schema: Any, path: str) -> Any:
    node = schema
    for token in path.split("."):
        collection = token.endswith("[]")
        key = token[:-2] if collection else token
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
        if collection:
            if not isinstance(node, list) or len(node) != 1:
                return None
            node = node[0]
    return node


def _schema_type_name(spec: Any) -> str:
    if isinstance(spec, str):
        return "enum" if spec.startswith("enum:") else spec
    if isinstance(spec, dict):
        return "object"
    if isinstance(spec, list):
        return "collection"
    return "unknown"


def validate_fixture_slot(
    slot: FixtureSlotSpec,
    schema: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if not slot.slot_id.strip():
        issues.append("slot_id is empty")
    if not slot.paths:
        issues.append(f"slot {slot.slot_id!r} has no schema paths")
    missing = [path for path in slot.paths if path not in schema]
    if missing:
        issues.append(
            f"slot {slot.slot_id!r} references unknown schema paths {missing!r}"
        )
    if not slot.material_kinds:
        issues.append(f"slot {slot.slot_id!r} accepts no material kinds")
    if slot.cardinality not in {"one", "singleton", "scenario", "collection"}:
        issues.append(
            f"slot {slot.slot_id!r} has invalid cardinality "
            f"{slot.cardinality!r}"
        )
    for field in slot.compiler_fields:
        if not _schema_path_exists(schema, field):
            issues.append(
                f"slot {slot.slot_id!r} compiler field {field!r} "
                "does not exist in FIXTURE_SCHEMA"
            )
        elif field.split(".", 1)[0].removesuffix("[]") not in slot.paths:
            issues.append(
                f"slot {slot.slot_id!r} compiler field {field!r} lies "
                "outside its selected paths"
            )
    return issues


def derive_fixture_slots(
    schema: dict[str, Any],
    overrides: dict[str, dict[str, Any]] | None = None,
    *,
    include_auto: bool = True,
) -> tuple[FixtureSlotSpec, ...]:
    """Derive top-level structural slots and merge semantic composites."""
    slots: dict[str, FixtureSlotSpec] = {}
    if include_auto:
        for key, branch in schema.items():
            cardinality = "collection" if isinstance(branch, list) else "singleton"
            slots[key] = FixtureSlotSpec(
                slot_id=key,
                paths=(key,),
                material_kinds=_auto_material_kinds(key),
                description=f"Auto-derived {cardinality} slot at fixture.{key}.",
                cardinality=cardinality,
            )
    for slot_id, raw in (overrides or {}).items():
        if not isinstance(raw, dict):
            raise ValueError(f"fixture slot {slot_id!r} must be an object")
        slot = _normalise_override(slot_id, raw)
        slots[slot_id] = slot
    issues = [
        issue for slot in slots.values()
        for issue in validate_fixture_slot(slot, schema)
    ]
    if issues:
        raise ValueError("; ".join(issues))
    return tuple(slots.values())


def schema_leaf_paths(schema: Any, prefix: str = "") -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(schema, str):
        return (prefix,)
    if isinstance(schema, dict):
        for key, child in schema.items():
            child_path = f"{prefix}.{key}" if prefix else key
            paths.extend(schema_leaf_paths(child, child_path))
    elif isinstance(schema, list) and len(schema) == 1:
        paths.extend(schema_leaf_paths(schema[0], prefix + "[]"))
    return tuple(paths)


def validate_fixture_template(
    template: FixtureTemplateSpec,
    *,
    require_complete_fields: bool | None = None,
) -> list[str]:
    issues: list[str] = []
    complete = template.migrated if require_complete_fields is None \
        else require_complete_fields
    leaves = set(schema_leaf_paths(template.schema))
    declared_paths = [field.path for field in template.fields]
    if len(declared_paths) != len(set(declared_paths)):
        issues.append("fixture template declares duplicate field paths")
    for field in template.fields:
        if field.path not in leaves:
            issues.append(f"field {field.path!r} does not exist in schema")
        if field.source not in FIELD_SOURCES:
            issues.append(
                f"field {field.path!r} has unknown source {field.source!r}"
            )
        if field.source == "model" and not field.meaning.strip():
            issues.append(
                f"model field {field.path!r} requires a meaning"
            )
        if complete and not field.format:
            issues.append(
                f"field {field.path!r} requires a format"
            )
        if field.format:
            allowed_types = FIELD_FORMAT_TYPES.get(field.format)
            if allowed_types is None:
                issues.append(
                    f"field {field.path!r} has unknown format {field.format!r}"
                )
            else:
                actual_type = _schema_type_name(
                    _schema_at_path(template.schema, field.path)
                )
                if actual_type not in allowed_types:
                    issues.append(
                        f"field {field.path!r} format {field.format!r} "
                        f"is incompatible with schema type {actual_type!r}"
                    )
    if complete:
        missing = sorted(leaves - set(declared_paths))
        if missing:
            issues.append(
                f"fixture template lacks field contracts for {missing!r}"
            )
    for slot in template.slots:
        issues.extend(validate_fixture_slot(slot, template.schema))
    return issues


def build_fixture_template(
    *,
    schema: dict[str, Any],
    slots: dict[str, dict[str, Any]] | None = None,
    fields: dict[str, dict[str, Any]] | None = None,
    migrated: bool = False,
) -> FixtureTemplateSpec:
    template = FixtureTemplateSpec(
        schema=copy.deepcopy(schema),
        fields=tuple(
            FixtureFieldSpec.from_dict(path, raw)
            for path, raw in (fields or {}).items()
        ),
        slots=derive_fixture_slots(
            schema, slots, include_auto=not migrated
        ),
        migrated=migrated,
    )
    issues = validate_fixture_template(template)
    if issues:
        raise ValueError("; ".join(issues))
    return template


RUNTIME_FIXTURE_TEMPLATES: dict[str, FixtureTemplateSpec] = {
    "runtime:user_chat": build_fixture_template(
        schema={
            "instruction": "str",
            "scripted_facts": "list",
        },
        fields={
            "instruction": {
                "source": "shared_context",
                "meaning": "Immutable instruction from the principal.",
                "format": "plain_text",
            },
            "scripted_facts": {
                "source": "private_runtime",
                "meaning": "Facts the principal may state in later turns.",
                "format": "string_list",
            },
        },
        slots={
            "instruction": {
                "path": "instruction",
                "material_kinds": ["instruction"],
                "description": "The principal's immutable instruction.",
                "cardinality": "singleton",
            },
            "later_user_fact": {
                "path": "scripted_facts",
                "material_kinds": [
                    "clarification_reply", "user_confirmation",
                    "user_fact", "chat_reply",
                ],
                "description": "A fact available through a later principal turn.",
                "cardinality": "scenario",
            },
        },
        migrated=True,
    ),
    "workspace_files": build_fixture_template(
        schema={
            "workspace_files": [{
                "path": "str",
                "content": "str",
            }],
            "tree_specs": [{
                "root": "str",
                "total_files": "int",
                "subdirs": "list",
                "extensions": "list",
                "key_files": [{
                    "path": "str",
                    "content": "str",
                }],
            }],
        },
        fields={
            "workspace_files[].path": {
                "source": "compiler",
                "meaning": "Absolute in-world path of one generated file.",
                "format": "absolute_path",
            },
            "workspace_files[].content": {
                "source": "model",
                "meaning": "Scenario-specific content of one workspace file.",
                "format": "plain_text",
            },
            "tree_specs[].root": {
                "source": "compiler",
                "meaning": "Absolute root directory of the generated file tree.",
                "format": "absolute_path",
            },
            "tree_specs[].total_files": {
                "source": "compiler",
                "meaning": "Total file count required by the material.",
                "format": "positive_integer",
            },
            "tree_specs[].subdirs": {
                "source": "compiler",
                "meaning": "Deterministic relative subdirectory layout.",
                "format": "string_list",
            },
            "tree_specs[].extensions": {
                "source": "compiler",
                "meaning": "Validated file extensions used for neutral filler.",
                "format": "string_list",
            },
            "tree_specs[].key_files[].path": {
                "source": "compiler",
                "meaning": "Relative path of one scenario-critical file.",
                "format": "relative_path",
            },
            "tree_specs[].key_files[].content": {
                "source": "model",
                "meaning": "Scenario-specific content of one critical file.",
                "format": "plain_text",
            },
        },
        slots={
            "single_file": {
                "path": "workspace_files",
                "material_kinds": ["document", "file"],
                "description": "One agent-visible workspace file.",
                "compiler_fields": ["workspace_files[].path"],
                "cardinality": "one",
            },
            "file_tree": {
                "path": "tree_specs",
                "material_kinds": ["file_tree", "workspace_files", "repo"],
                "description": "A directory with critical files and deterministic filler.",
                "compiler_fields": [
                    "tree_specs[].root",
                    "tree_specs[].total_files",
                    "tree_specs[].subdirs",
                    "tree_specs[].extensions",
                    "tree_specs[].key_files[].path",
                ],
                "cardinality": "one",
            },
        },
        migrated=True,
    ),
    "prior_memory": build_fixture_template(
        schema={"prior_memory_md": "str"},
        fields={
            "prior_memory_md": {
                "source": "model",
                "meaning": "Agent-visible memory content from earlier sessions.",
                "format": "markdown",
            },
        },
        slots={
            "prior_memory": {
                "path": "prior_memory_md",
                "material_kinds": ["*"],
                "description": "Prompt-injected prior-session memory.",
                "cardinality": "singleton",
            },
        },
        migrated=True,
    ),
    "runtime:shell": build_fixture_template(
        schema={
            "shell_env": {"home": "str", "cwd": "str"},
            "workspace_files": [{"path": "str", "content": "str"}],
            "environment_facts": "list",
        },
        fields={
            "shell_env.home": {
                "source": "shared_context",
                "meaning": "Agent shell HOME directory.",
                "format": "absolute_path",
            },
            "shell_env.cwd": {
                "source": "shared_context",
                "meaning": "Agent shell initial working directory.",
                "format": "absolute_path",
            },
            "workspace_files[].path": {
                "source": "compiler",
                "meaning": "Absolute path of a shell profile or scenario file.",
                "format": "absolute_path",
            },
            "workspace_files[].content": {
                "source": "model",
                "meaning": "Content of a scenario-required shell profile file.",
                "format": "plain_text",
            },
            "environment_facts": {
                "source": "compiler",
                "meaning": "Verified non-text runtime facts such as absence or VCS state.",
                "format": "json_list",
            },
        },
        slots={
            "shell_profile": {
                "paths": ["shell_env", "workspace_files", "environment_facts"],
                "material_kinds": ["*"],
                "description": "Shell identity, profile files, and verified runtime facts.",
                "compiler_fields": [
                    "workspace_files[].path",
                    "environment_facts",
                ],
                "cardinality": "singleton",
            },
        },
        migrated=True,
    ),
}

RUNTIME_SLOTS: dict[str, tuple[FixtureSlotSpec, ...]] = {
    carrier: template.slots
    for carrier, template in RUNTIME_FIXTURE_TEMPLATES.items()
}


def runtime_fixture_template(carrier: str) -> FixtureTemplateSpec | None:
    return RUNTIME_FIXTURE_TEMPLATES.get(carrier)


def render_fixture_slots(
    templates: Iterable[tuple[str, dict[str, Any], Iterable[FixtureSlotSpec]]],
) -> str:
    blocks: list[str] = []
    for template, schema, slots in templates:
        lines = [f"### {template} fixture slots"]
        for slot in slots:
            data = slot.to_dict(schema)
            lines.append(
                f"- {slot.slot_id}: paths={data['paths']}; "
                f"material_kinds={data['material_kinds']}; "
                f"compiler_fields={data['compiler_fields']}; "
                f"description={slot.description or '(structural slot)'}"
            )
            lines.append(
                "  content_schema="
                + json.dumps(data["content_schema"], ensure_ascii=False)
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def runtime_slot(carrier: str, slot_id: str) -> FixtureSlotSpec | None:
    return next(
        (slot for slot in RUNTIME_SLOTS.get(carrier, ())
         if slot.slot_id == slot_id),
        None,
    )


def compatible_runtime_slots(
    carrier: str,
    material_kind: str,
) -> tuple[FixtureSlotSpec, ...]:
    return tuple(
        slot for slot in RUNTIME_SLOTS.get(carrier, ())
        if slot.accepts(material_kind)
    )


def minimal_fixture_for_slots(
    schema: dict[str, Any],
    slots: Iterable[FixtureSlotSpec],
) -> dict[str, Any]:
    """Instantiate only selected top-level branches, with no fake content."""
    fixture: dict[str, Any] = {}
    for slot in slots:
        for path in slot.paths:
            branch = schema[path]
            if isinstance(branch, list):
                fixture.setdefault(path, [])
            elif isinstance(branch, dict):
                fixture.setdefault(path, {})
            # Scalar content is omitted until M2 supplies a real value.
    return fixture


def _evidence_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if value is None:
        return []
    if isinstance(value, dict):
        return [
            leaf for child in value.values() for leaf in _evidence_leaves(child)
        ]
    if isinstance(value, list):
        return [leaf for child in value for leaf in _evidence_leaves(child)]
    return [json.dumps(value, ensure_ascii=False)]


def _schema_value_path_present(node: Any, tokens: list[str]) -> bool:
    if not tokens:
        return True
    token, rest = tokens[0], tokens[1:]
    if token.endswith("[]"):
        key = token[:-2]
        if not isinstance(node, dict) or not isinstance(node.get(key), list):
            return False
        return any(
            _schema_value_path_present(item, rest) for item in node[key]
        )
    if not isinstance(node, dict) or token not in node:
        return False
    return _schema_value_path_present(node[token], rest)


def _strict_shape_issues(value: Any, spec: Any, path: str) -> list[str]:
    from services._base import validate_fixture

    issues = validate_fixture({path: value}, {path: spec})

    def walk(current: Any, expected: Any, where: str) -> None:
        if isinstance(expected, dict) and isinstance(current, dict):
            for key in sorted(set(current) - set(expected)):
                issues.append(f"{where}: unknown field {key!r}")
            for key in set(current) & set(expected):
                walk(current[key], expected[key], f"{where}.{key}")
        elif (isinstance(expected, list) and len(expected) == 1
              and isinstance(current, list)):
            for index, item in enumerate(current):
                walk(item, expected[0], f"{where}[{index}]")

    walk(value, spec, path)
    return issues


def _merge_object(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_object(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _assign_compiler_field(
    node: Any,
    tokens: list[str],
    *,
    material_id: str,
    ordinal: int = 0,
    field_path: str = "",
) -> None:
    if not tokens or not isinstance(node, dict):
        return
    token, rest = tokens[0], tokens[1:]
    current_path = f"{field_path}.{token}" if field_path else token
    if token.endswith("[]"):
        key = token[:-2]
        values = node.get(key)
        if not isinstance(values, list):
            return
        for index, child in enumerate(values):
            _assign_compiler_field(
                child, rest,
                material_id=material_id,
                ordinal=index,
                field_path=current_path,
            )
        return
    if rest:
        _assign_compiler_field(
            node.get(token), rest,
            material_id=material_id, ordinal=ordinal,
            field_path=current_path,
        )
        return
    digest = hashlib.sha256(
        f"{material_id}\0{current_path}\0{ordinal}".encode()
    ).hexdigest()[:10]
    node[token] = f"record-{digest}"


def compile_slot_payloads(
    *,
    schema: dict[str, Any],
    slots: Iterable[FixtureSlotSpec],
    bindings: list[dict[str, Any]],
    payloads: list[dict[str, Any]],
    base_fixture: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """Compile model-authored slot values into a carrier fixture."""
    fixture = copy.deepcopy(base_fixture or {})
    slot_by_id = {slot.slot_id: slot for slot in slots}
    binding_ids = [str(binding.get("material_id")) for binding in bindings]
    payload_by_material: dict[str, dict[str, Any]] = {}
    placements: list[dict[str, str]] = []
    issues: list[str] = []
    payload_counts: dict[str, int] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            issues.append("slot payload must be an object")
            continue
        material_id = str(payload.get("material_id"))
        payload_counts[material_id] = payload_counts.get(material_id, 0) + 1
        payload_by_material.setdefault(material_id, payload)
    for material_id, count in payload_counts.items():
        if material_id not in binding_ids:
            issues.append(f"payload references unknown material {material_id!r}")
        if count > 1:
            issues.append(
                f"material {material_id!r} has {count} slot payloads; expected one"
            )
    claimed_singletons: dict[str, str] = {}

    for binding in bindings:
        material_id = str(binding.get("material_id"))
        expected_slot = str(binding.get("fixture_slot"))
        slot = slot_by_id.get(expected_slot)
        payload = payload_by_material.get(material_id)
        if slot is None:
            issues.append(
                f"{material_id}: unknown compiled fixture slot {expected_slot!r}"
            )
            continue
        if payload is None:
            issues.append(f"{material_id}: no slot payload returned")
            continue
        if str(payload.get("fixture_slot")) != expected_slot:
            issues.append(
                f"{material_id}: payload slot {payload.get('fixture_slot')!r} "
                f"does not match binding {expected_slot!r}"
            )
            continue
        values = payload.get("values")
        if not isinstance(values, dict):
            issues.append(f"{material_id}: slot values must be an object")
            continue
        extra = sorted(set(values) - set(slot.paths))
        if extra:
            issues.append(
                f"{material_id}: slot payload writes paths outside "
                f"{expected_slot!r}: {extra!r}"
            )
            continue
        values = copy.deepcopy(values)
        for compiler_field in slot.compiler_fields:
            if _schema_value_path_present(
                values, compiler_field.split(".")
            ):
                issues.append(
                    f"{material_id}: payload authored compiler-owned field "
                    f"{compiler_field!r}"
                )
            _assign_compiler_field(
                values,
                compiler_field.split("."),
                material_id=material_id,
            )

        realized_paths: list[str] = []
        authored: dict[str, Any] = {}
        for path, value in values.items():
            branch = schema[path]
            shape_issues = _strict_shape_issues(value, branch, path)
            if shape_issues:
                issues.extend(
                    f"{material_id}: {issue}" for issue in shape_issues
                )
                continue
            authored[path] = value
            if isinstance(branch, list):
                if not isinstance(value, list):
                    issues.append(f"{material_id}: {path} must be a list")
                    continue
                start = len(fixture.setdefault(path, []))
                fixture[path].extend(copy.deepcopy(value))
                realized_paths.extend(
                    f"/{path}/{index}" for index in range(start, len(fixture[path]))
                )
            elif isinstance(branch, dict):
                previous_owner = claimed_singletons.get(path)
                if previous_owner and previous_owner != material_id:
                    issues.append(
                        f"{material_id}: singleton path {path!r} already "
                        f"claimed by material {previous_owner!r}"
                    )
                    continue
                claimed_singletons[path] = material_id
                if not isinstance(value, dict):
                    issues.append(f"{material_id}: {path} must be an object")
                    continue
                existing = fixture.setdefault(path, {})
                if not isinstance(existing, dict):
                    issues.append(f"{material_id}: base fixture {path} is not an object")
                    continue
                _merge_object(existing, value)
                realized_paths.append(f"/{path}")
            else:
                previous_owner = claimed_singletons.get(path)
                if previous_owner and previous_owner != material_id:
                    issues.append(
                        f"{material_id}: scalar path {path!r} already "
                        f"claimed by material {previous_owner!r}"
                    )
                    continue
                claimed_singletons[path] = material_id
                fixture[path] = copy.deepcopy(value)
                realized_paths.append(f"/{path}")

        leaves = _evidence_leaves(authored)
        quote = max(leaves, key=len) if leaves else ""
        if not realized_paths:
            issues.append(f"{material_id}: slot payload realized no schema path")
            continue
        if not quote:
            issues.append(
                f"{material_id}: slot payload has no string evidence for placement"
            )
            continue
        placements.append({
            "material_id": material_id,
            "location": ", ".join(realized_paths),
            "content_quote": quote,
        })

    return fixture, placements, issues


def _merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def apply_slot_patches(
    previous: list[dict[str, Any]],
    patches: list[dict[str, Any]],
    *,
    allowed_materials: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply material-scoped merge patches without replacing other slots."""
    result = copy.deepcopy(previous)
    by_material = {
        str(payload.get("material_id")): payload for payload in result
    }
    issues: list[str] = []
    seen: set[str] = set()
    for item in patches:
        if not isinstance(item, dict):
            issues.append("slot patch must be an object")
            continue
        material_id = str(item.get("material_id"))
        if material_id not in allowed_materials:
            issues.append(
                f"patch targets non-failing material {material_id!r}"
            )
            continue
        payload = by_material.get(material_id)
        if payload is None:
            issues.append(f"patch targets unknown material {material_id!r}")
            continue
        patch = item.get("values_patch")
        if not isinstance(patch, dict):
            issues.append(f"{material_id}: values_patch must be an object")
            continue
        payload["values"] = _merge_patch(payload.get("values"), patch)
        seen.add(material_id)
    missing = sorted(allowed_materials - seen)
    if missing:
        issues.append(f"no slot patch returned for failing materials {missing!r}")
    return result, issues


def restore_slot_payloads(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    material_ids: set[str],
) -> list[dict[str, Any]]:
    """Rollback selected materials while preserving accepted sibling slots."""
    old = {
        str(payload.get("material_id")): copy.deepcopy(payload)
        for payload in previous
    }
    restored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in current:
        mid = str(payload.get("material_id"))
        if mid in material_ids and mid in old:
            restored.append(copy.deepcopy(old[mid]))
            seen.add(mid)
        else:
            restored.append(copy.deepcopy(payload))
    for mid in material_ids - seen:
        if mid in old:
            restored.append(copy.deepcopy(old[mid]))
    return restored


def canonicalize_fixture(
    data: Any,
    schema: Any,
    path: str = "",
) -> list[str]:
    """Apply schema-wide, semantics-preserving canonicalization in place."""
    changes: list[str] = []
    if isinstance(data, dict) and isinstance(schema, dict):
        for key, child_schema in schema.items():
            if key not in data:
                continue
            child_path = f"{path}.{key}" if path else key
            if data[key] is None:
                del data[key]
                changes.append(
                    f"treated optional null field {child_path} as absent"
                )
                continue
            changes.extend(
                canonicalize_fixture(data[key], child_schema, child_path)
            )
    elif (isinstance(data, list) and isinstance(schema, list)
          and len(schema) == 1):
        for index, item in enumerate(data):
            changes.extend(
                canonicalize_fixture(item, schema[0], f"{path}[{index}]")
            )
    return changes


def path_root(path: str) -> str:
    """Return the exact registered top-level schema branch for a field path."""
    return path.split(".", 1)[0].removesuffix("[]")


def nested_collection_path(field_path: str) -> str:
    """Return the collection path owning a field, or empty for scalars."""
    tokens = field_path.split(".")
    last = max(
        (index for index, token in enumerate(tokens) if token.endswith("[]")),
        default=-1,
    )
    if last < 0:
        return ""
    tokens[last] = tokens[last].removesuffix("[]")
    return ".".join(tokens[:last + 1])


def slot_path_index(
    slots: Iterable[FixtureSlotSpec],
) -> dict[str, tuple[FixtureSlotSpec, ...]]:
    """Map each exact slot path to every slot that declares it."""
    index: dict[str, list[FixtureSlotSpec]] = {}
    for slot in slots:
        for path in slot.paths:
            index.setdefault(path, []).append(slot)
    return {path: tuple(owners) for path, owners in index.items()}


def overlapping_slot_issues(
    slots: Iterable[FixtureSlotSpec],
    schema: dict[str, Any] | None = None,
) -> list[str]:
    """Fail closed when overlapping slots disagree about a shared path.

    Slot ``cardinality`` describes the slot as a whole. A composite slot may
    include both singleton identity fields and a collection. Structural
    identity of a shared path comes from FIXTURE_SCHEMA, not from mixing
    those slot-level labels.
    """
    issues: list[str] = []
    if schema is None:
        return issues
    index = slot_path_index(slots)
    for path, owners in index.items():
        if path not in schema:
            issues.append(
                f"overlapping slots {sorted(slot.slot_id for slot in owners)!r} "
                f"share unregistered path {path!r}"
            )
            continue
        if len(owners) < 2:
            continue
        branch = schema[path]
        if not isinstance(branch, (list, dict, str)):
            issues.append(
                f"overlapping slots {sorted(slot.slot_id for slot in owners)!r} "
                f"share path {path!r} with unknown schema structure"
            )
    return issues

