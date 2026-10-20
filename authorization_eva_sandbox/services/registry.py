"""Service registry loader — the machine-readable catalog of service templates.

Loads ``services/service_catalog.json`` into a queryable registry. Two data
sources are merged:

1. The draft JSON: categories, template descriptions, bespoke mechanics,
   affordance candidates, conversion status.
2. The live FastAPI route tables of converted services: the AUTHORITATIVE
   per-template action inventory (name, method, path, summary, request
   params) is derived from each service's merged OpenAPI schema, never
   hand-maintained.

Consumers:
- S2 vignette generation: ``grounding_slice()`` renders a compact prompt
  block for the chosen templates (actions + affordances only, never the
  whole registry — prompt length control per plan §3).
- S2/S4 grounding checks: ``resolve_action()`` verifies that every
  ``violated_actions[].action`` and every golden_path step binds to a real
  action on a mounted template.
- S4 materialization: ``fixture_schema`` (live-derived from each service
  class's FIXTURE_SCHEMA attribute) is the fixture contract for content
  generation prompts and D-gate validation.

CLI: ``python services/registry.py`` prints the derived inventory and
verifies that every converted template exports at least one action.
"""
from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_SERVICES_DIR = Path(__file__).resolve().parent
_SANDBOX_ROOT = _SERVICES_DIR.parent
sys.path.insert(0, str(_SANDBOX_ROOT))

from services._base import INFRA_SUFFIXES, ServiceBase  # noqa: E402
from services.fixture_slots import (  # noqa: E402
    FixtureTemplateSpec,
    FixtureSlotSpec,
    build_fixture_template,
)
from services.contracts import (  # noqa: E402
    ActionContractSpec,
    validate_contract_declarations,
)

DEFAULT_REGISTRY_PATH = _SERVICES_DIR / "service_catalog.json"


@dataclass
class ActionSpec:
    """One agent-facing action, derived from a live FastAPI route."""

    name: str                      # operation_id (== audit-log action name by convention)
    method: str                    # HTTP method, upper-case
    path: str                      # route path relative to the service mount
    summary: str = ""
    params: dict[str, Any] = field(default_factory=dict)   # JSON-schema properties
    required: list[str] = field(default_factory=list)
    contract: Optional[ActionContractSpec] = None

    def signature(self) -> str:
        """One-line human/prompt-friendly signature."""
        parts = []
        for pname, pschema in self.params.items():
            ptype = pschema.get("type", "any") if isinstance(pschema, dict) else "any"
            mark = "" if pname in self.required else "?"
            parts.append(f"{pname}{mark}: {ptype}")
        sig = f"{self.name}({', '.join(parts)})"
        return f"{sig} — {self.summary}" if self.summary else sig

    def to_dict(self) -> dict:
        result = {
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "params": self.params,
            "required": self.required,
        }
        if self.contract is not None:
            result["contract"] = self.contract.to_dict()
        return result


@dataclass
class TemplateSpec:
    """One canonical catalog entry plus implementation-derived contracts."""

    template: str
    category: str
    description: str = ""
    converted: bool = False        # service.py exists on disk (authoritative signal)
    # Live-derived from the service class's FIXTURE_SCHEMA attribute (the
    # declared fixture contract next to load_data); None if not declared.
    fixture_template: Optional[FixtureTemplateSpec] = None
    capability_notes: list[dict[str, Any]] = field(default_factory=list)
    actions: list[ActionSpec] = field(default_factory=list)     # live-derived; [] if not converted
    capability_profiles: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    environment_actions: dict[str, ActionContractSpec] = field(
        default_factory=dict
    )

    def action_names(self) -> list[str]:
        return [a.name for a in self.actions]

    @property
    def fixture_schema(self) -> Optional[dict]:
        return self.fixture_template.schema if self.fixture_template else None

    @property
    def fixture_slots(self) -> tuple[FixtureSlotSpec, ...]:
        return self.fixture_template.slots if self.fixture_template else ()


def _resolve_ref(schema: dict, root: dict) -> dict:
    """Follow a local $ref chain into the OpenAPI components section."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 10:
        ref = schema["$ref"]
        node: Any = root
        for part in ref.lstrip("#/").split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
        seen += 1
    return schema if isinstance(schema, dict) else {}


def derive_actions(template: str) -> list[ActionSpec]:
    """Derive the action inventory from a converted service's live routes.

    Boots the service with an empty fixture (ServiceBase guarantees this
    works), mounts its router on a throwaway FastAPI app, and walks the
    generated OpenAPI schema. Infra routes are excluded exactly as the
    tool-IR generator excludes them.
    """
    from fastapi import FastAPI

    module = importlib.import_module(f"services.{template}.service")
    svc = module.create_service(template)
    app = FastAPI()
    app.include_router(svc.router)
    schema = app.openapi()

    actions: list[ActionSpec] = []
    for path, methods in schema.get("paths", {}).items():
        if path.endswith(INFRA_SUFFIXES):
            continue
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            params: dict[str, Any] = {}
            required: list[str] = []
            body = op.get("requestBody", {})
            body_schema = _resolve_ref(
                body.get("content", {}).get("application/json", {}).get("schema", {}),
                schema,
            )
            if body_schema:
                params.update(body_schema.get("properties", {}))
                required.extend(body_schema.get("required", []))
            for qp in op.get("parameters", []):
                params[qp["name"]] = qp.get("schema", {})
                if qp.get("required"):
                    required.append(qp["name"])
            actions.append(ActionSpec(
                name=op.get("operationId") or f"{method}_{path.strip('/').replace('/', '_')}",
                method=method.upper(),
                path=path,
                summary=op.get("summary", ""),
                params=params,
                required=required,
            ))
    return actions


def _service_class(template: str) -> type[ServiceBase] | None:
    module = importlib.import_module(f"services.{template}.service")
    for obj in vars(module).values():
        if (isinstance(obj, type) and issubclass(obj, ServiceBase)
                and obj is not ServiceBase):
            return obj
    return None


def derive_fixture_template(template: str) -> Optional[FixtureTemplateSpec]:
    """Load one canonical template, with a temporary legacy fallback."""
    service_class = _service_class(template)
    if service_class is None:
        return None
    declaration = getattr(service_class, "FIXTURE_TEMPLATE", None)
    if isinstance(declaration, dict):
        schema = declaration.get("schema")
        if not isinstance(schema, dict):
            raise ValueError(f"{template}: FIXTURE_TEMPLATE.schema must be an object")
        return build_fixture_template(
            schema=schema,
            slots=declaration.get("materialization_slots"),
            fields=declaration.get("fields"),
            migrated=bool(declaration.get("migrated", True)),
        )
    schema = getattr(service_class, "FIXTURE_SCHEMA", None)
    if not isinstance(schema, dict):
        return None
    slots = getattr(service_class, "FIXTURE_SLOTS", None)
    return build_fixture_template(
        schema=schema,
        slots=slots if isinstance(slots, dict) else None,
        migrated=False,
    )


def derive_fixture_schema(template: str) -> Optional[dict]:
    """Read the service's canonical or legacy fixture schema.

    The schema lives in service.py next to load_data — the registry never
    stores a second copy. Returns None for templates without a fixture
    contract (e.g. config-driven services like user_messaging)."""
    fixture_template = derive_fixture_template(template)
    return dict(fixture_template.schema) if fixture_template else None


def derive_template_fixture_slots(
    template: str,
    schema: Optional[dict] = None,
) -> tuple[FixtureSlotSpec, ...]:
    """Read slots from the canonical template (legacy compatible)."""
    fixture_template = derive_fixture_template(template)
    return fixture_template.slots if fixture_template else ()


def validate_catalog_data(data: dict[str, Any]) -> list[str]:
    """Fail-closed validation for the canonical active service catalog."""
    issues: list[str] = []
    if set(data) != {"schema_version", "categories", "templates"}:
        issues.append(
            "catalog root fields must be schema_version/categories/templates"
        )
    if data.get("schema_version") != "v4-service-catalog-1":
        issues.append("catalog schema_version must be v4-service-catalog-1")
    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        issues.append("categories must be a non-empty object")
        categories = {}
    for category, description in categories.items():
        if not str(category).strip() or not str(description).strip():
            issues.append(f"category {category!r} requires a description")
    templates = data.get("templates")
    if not isinstance(templates, list):
        issues.append("templates must be a list")
        return issues
    allowed_template_fields = {
        "template", "category", "description", "capability_notes"
    }
    seen_templates: set[str] = set()
    for index, entry in enumerate(templates):
        where = f"templates[{index}]"
        if not isinstance(entry, dict):
            issues.append(f"{where} must be an object")
            continue
        unknown = sorted(set(entry) - allowed_template_fields)
        missing = sorted(
            {"template", "category", "description"} - set(entry)
        )
        if unknown:
            issues.append(f"{where} has unknown fields {unknown!r}")
        if missing:
            issues.append(f"{where} lacks fields {missing!r}")
        template = str(entry.get("template") or "")
        if not template:
            issues.append(f"{where}.template is empty")
        elif template in seen_templates:
            issues.append(f"duplicate template {template!r}")
        seen_templates.add(template)
        category = str(entry.get("category") or "")
        if category not in categories:
            issues.append(f"{where}.category {category!r} is unknown")
        if not str(entry.get("description") or "").strip():
            issues.append(f"{where}.description is empty")
        notes = entry.get("capability_notes", [])
        if not isinstance(notes, list):
            issues.append(f"{where}.capability_notes must be a list")
            continue
        note_ids: set[str] = set()
        for note_index, note in enumerate(notes):
            note_where = f"{where}.capability_notes[{note_index}]"
            if not isinstance(note, dict):
                issues.append(f"{note_where} must be an object")
                continue
            if set(note) != {"id", "summary", "action_refs"}:
                issues.append(
                    f"{note_where} fields must be id/summary/action_refs"
                )
            note_id = str(note.get("id") or "")
            if not note_id or note_id in note_ids:
                issues.append(f"{note_where}.id is empty or duplicate")
            note_ids.add(note_id)
            if not str(note.get("summary") or "").strip():
                issues.append(f"{note_where}.summary is empty")
            refs = note.get("action_refs")
            if (
                not isinstance(refs, list)
                or not refs
                or not all(isinstance(ref, str) and ref for ref in refs)
            ):
                issues.append(
                    f"{note_where}.action_refs must be non-empty strings"
                )
    return issues


class ServiceRegistry:
    """Queryable view over the template catalog + live action inventories."""

    def __init__(self, data: dict, derive_live: bool = True):
        catalog_issues = validate_catalog_data(data)
        if catalog_issues:
            raise ValueError(
                "invalid service catalog: " + "; ".join(catalog_issues)
            )
        self.categories: dict[str, str] = dict(data.get("categories", {}))
        self.templates: dict[str, TemplateSpec] = {}
        for entry in data.get("templates", []):
            name = entry["template"]
            converted = (_SERVICES_DIR / name / "service.py").is_file()
            spec = TemplateSpec(
                template=name,
                category=entry.get("category", ""),
                description=entry.get("description", ""),
                converted=converted,
                capability_notes=list(entry.get("capability_notes", [])),
            )
            if converted and derive_live:
                spec.actions = derive_actions(name)
                spec.fixture_template = derive_fixture_template(name)
                service_class = _service_class(name)
                contracts = getattr(
                    service_class, "ACTION_CONTRACTS", None
                )
                profiles = getattr(
                    service_class, "CAPABILITY_PROFILES", None
                )
                environment = getattr(
                    service_class, "ENVIRONMENT_ACTION_CONTRACTS", {}
                )
                if isinstance(contracts, dict) or isinstance(profiles, dict):
                    if not isinstance(contracts, dict) or not isinstance(
                        profiles, dict
                    ):
                        raise ValueError(
                            f"{name}: contracts and profiles must both be declared"
                        )
                    issues = validate_contract_declarations(
                        live_actions={action.name for action in spec.actions},
                        contracts=contracts,
                        profiles=profiles,
                    )
                    if issues:
                        raise ValueError(
                            f"{name}: " + "; ".join(issues)
                        )
                    for action in spec.actions:
                        action.contract = contracts[action.name]
                    spec.capability_profiles = {
                        str(profile): tuple(actions)
                        for profile, actions in profiles.items()
                    }
                    spec.environment_actions = (
                        dict(environment)
                        if isinstance(environment, dict) else {}
                    )
                live_action_names = {action.name for action in spec.actions}
                for note in spec.capability_notes:
                    missing_refs = sorted(
                        set(note.get("action_refs") or [])
                        - live_action_names
                    )
                    if missing_refs:
                        raise ValueError(
                            f"{name}: capability note {note.get('id')!r} "
                            f"references non-live actions {missing_refs!r}"
                        )
            self.templates[name] = spec

    @classmethod
    def load(cls, path: Path | str = DEFAULT_REGISTRY_PATH, derive_live: bool = True) -> "ServiceRegistry":
        data = json.loads(Path(path).read_text())
        return cls(data, derive_live=derive_live)

    # ── queries ──────────────────────────────────────────────────────────────

    def get(self, template: str) -> TemplateSpec:
        return self.templates[template]

    def converted_templates(self) -> list[str]:
        return [t for t, s in self.templates.items() if s.converted]

    def templates_in_category(self, category: str) -> list[str]:
        return [t for t, s in self.templates.items() if s.category == category]

    def resolve_action(self, template: str, action: str) -> Optional[ActionSpec]:
        """Bind an action name to a live ActionSpec; None if it doesn't exist."""
        spec = self.templates.get(template)
        if spec is None:
            return None
        for act in spec.actions:
            if act.name == action:
                return act
        return None

    def resolve_fixture_slot(
        self,
        template: str,
        slot_id: str,
        material_kind: str | None = None,
    ) -> Optional[FixtureSlotSpec]:
        spec = self.templates.get(template)
        if spec is None:
            return None
        for slot in spec.fixture_slots:
            if slot.slot_id != slot_id:
                continue
            if material_kind is not None and not slot.accepts(material_kind):
                return None
            return slot
        return None

    def compatible_fixture_slots(
        self,
        template: str,
        material_kind: str,
    ) -> tuple[FixtureSlotSpec, ...]:
        spec = self.templates.get(template)
        if spec is None:
            return ()
        return tuple(
            slot for slot in spec.fixture_slots
            if slot.accepts(material_kind)
        )

    # ── prompt-facing rendering (S2 grounding slice) ─────────────────────────

    def grounding_slice(self, templates: list[str]) -> str:
        """Compact prompt block for the CHOSEN templates only (plan §3:
        never inject the whole registry into a generation prompt)."""
        blocks: list[str] = []
        for name in templates:
            spec = self.templates.get(name)
            if spec is None:
                blocks.append(f"### {name}\n(unknown template — not in registry)")
                continue
            lines = [f"### {name}  [{spec.category}]", spec.description]
            if spec.actions:
                lines.append("Actions:")
                lines.extend(f"  - {a.signature()}" for a in spec.actions)
            if spec.capability_notes:
                lines.append("Capability notes:")
                lines.extend(
                    "  - "
                    + note["summary"]
                    + " ["
                    + ", ".join(note["action_refs"])
                    + "]"
                    for note in spec.capability_notes
                )
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    # ── export ───────────────────────────────────────────────────────────────

    def export_inventory(self) -> dict:
        """Full machine-readable inventory (for audits / snapshot tests)."""
        return {
            "categories": self.categories,
            "templates": {
                t: {
                    "category": s.category,
                    "converted": s.converted,
                    "actions": [a.to_dict() for a in s.actions],
                    "description": s.description,
                    "capability_notes": s.capability_notes,
                    "fixture_template": (
                        s.fixture_template.to_dict()
                        if s.fixture_template else None
                    ),
                    "capability_profiles": {
                        profile: list(actions)
                        for profile, actions in sorted(
                            s.capability_profiles.items()
                        )
                    },
                }
                for t, s in sorted(self.templates.items())
            },
        }


def main() -> int:
    registry = ServiceRegistry.load()
    converted = registry.converted_templates()
    failures = []
    print(f"registry: {len(registry.templates)} templates, {len(converted)} converted\n")
    for name in converted:
        spec = registry.get(name)
        status = "OK " if spec.actions else "FAIL"
        if not spec.actions:
            failures.append(name)
        print(f"[{status}] {name:28s} {len(spec.actions):2d} actions: "
              + ", ".join(a.name for a in spec.actions))
    if failures:
        print(f"\nFAIL: converted templates with no derivable actions: {failures}")
        return 1
    print("\nAll converted templates export a live action inventory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
