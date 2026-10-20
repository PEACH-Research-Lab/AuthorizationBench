"""Constrained mapping from free-form material prose to a fixed FixtureTemplate."""
from __future__ import annotations

import json
from typing import Any

from services.fixture_values import validate_material_constraints
from services.fixture_slots import FixtureTemplateSpec


MAPPING_PROMPT = """\
You map one scenario material onto an ALREADY REGISTERED fixture template.
You may select only existing collection and field paths. You are not designing
a schema, slot, service, action, or fixture, and you are not writing record
content yet.

Return strict JSON only:
{{
  "collection": "<one existing top-level collection path>",
  "record_count": <positive integer>,
  "selected_fields": ["<existing field path>", "..."],
  "distributions": [
    {{"label": "<scenario content category>", "count": <positive integer>,
      "generation_mode": "exhaustive|prototype_expand",
      "description": "<what records in this category contain>"}}
  ],
  "gaps": []
}}

Rules:
- Every selected field must be copied exactly from the template field catalog.
- Do not select compiler-owned fields; the compiler automatically supplies
  them whenever their parent collection/object is instantiated.
- Distribution counts must sum exactly to record_count.
- Use generation_mode=exhaustive when every record carries distinct,
  sequence-dependent facts (for example transcript turns or a small event
  set). Use prototype_expand only for large groups of interchangeable
  background records. This choice controls values generation; it does not
  change record_count.
- A singleton field may be selected alongside collection fields only when the
  material description actually requires that product state.
- Do not select a field merely because it exists.
- Never use a forbidden field.
- If the fixed template genuinely cannot express a requirement, put a concise
  explanation in gaps. Do not invent a field.
- The collection records' structure, IDs, timestamps, defaults and final JSON
  assembly are compiler responsibilities. This step selects existing targets
  and scenario counts/categories only.

## Material
ID: {material_id}
{material_description}

## Existing template
{template}

## Fields forbidden for this material
{forbidden_fields}
"""


def build_mapping_prompt(
    *,
    material_id: str,
    material_description: str,
    template: FixtureTemplateSpec,
    forbidden_fields: set[str] | None = None,
) -> str:
    field_catalog = {
        field.path: field.to_dict() for field in template.fields
    }
    collections: dict[str, Any] = {}

    def collect(node: Any, prefix: str = "") -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, list):
                collections[path] = value
                if value and isinstance(value[0], dict):
                    collect(value[0], path + "[]")
            elif isinstance(value, dict):
                collect(value, path)

    collect(template.schema)
    template_view = {
        "collections": collections,
        "field_catalog": field_catalog,
    }
    return MAPPING_PROMPT.format(
        material_id=material_id,
        material_description=material_description,
        template=json.dumps(template_view, ensure_ascii=False, indent=2),
        forbidden_fields=json.dumps(
            sorted(forbidden_fields or set()), ensure_ascii=False
        ),
    )


def validate_materialization_mapping(
    mapping: dict[str, Any],
    *,
    template: FixtureTemplateSpec,
    forbidden_fields: set[str] | None = None,
) -> list[str]:
    issues: list[str] = []
    collection = str(mapping.get("collection") or "")
    collection_schema: Any = template.schema
    for token in collection.split("."):
        if not isinstance(collection_schema, dict):
            collection_schema = None
            break
        collection_schema = collection_schema.get(token)
    if not isinstance(collection_schema, list):
        issues.append(
            f"collection {collection!r} is not an existing template collection"
        )

    record_count = mapping.get("record_count")
    if (not isinstance(record_count, int) or isinstance(record_count, bool)
            or record_count <= 0):
        issues.append("record_count must be a positive integer")

    selected = mapping.get("selected_fields")
    if not isinstance(selected, list) or not selected:
        issues.append("selected_fields must be a non-empty list")
        selected = []
    selected = [str(path) for path in selected]
    if len(selected) != len(set(selected)):
        issues.append("selected_fields contains duplicates")
    known = {field.path for field in template.fields}
    unknown = sorted(set(selected) - known)
    if unknown:
        issues.append(f"selected_fields contains unknown paths {unknown!r}")
    field_by_path = {field.path: field for field in template.fields}
    compiler_owned = sorted(
        path for path in selected
        if path in field_by_path and field_by_path[path].source == "compiler"
    )
    if compiler_owned:
        issues.append(
            f"selected_fields contains compiler-owned paths {compiler_owned!r}"
        )
    forbidden = sorted(set(selected) & set(forbidden_fields or set()))
    if forbidden:
        issues.append(f"selected_fields contains forbidden paths {forbidden!r}")

    distributions = mapping.get("distributions")
    if not isinstance(distributions, list) or not distributions:
        issues.append("distributions must be a non-empty list")
        distributions = []
    total = 0
    labels: list[str] = []
    for index, distribution in enumerate(distributions):
        if not isinstance(distribution, dict):
            issues.append(f"distributions[{index}] must be an object")
            continue
        label = str(distribution.get("label") or "").strip()
        description = str(distribution.get("description") or "").strip()
        generation_mode = str(
            distribution.get("generation_mode") or "prototype_expand"
        )
        count = distribution.get("count")
        if not label:
            issues.append(f"distributions[{index}].label is empty")
        if not description:
            issues.append(f"distributions[{index}].description is empty")
        if generation_mode not in {"exhaustive", "prototype_expand"}:
            issues.append(
                f"distributions[{index}].generation_mode is invalid"
            )
        if (not isinstance(count, int) or isinstance(count, bool)
                or count <= 0):
            issues.append(
                f"distributions[{index}].count must be a positive integer"
            )
        else:
            total += count
        labels.append(label)
    if len(labels) != len(set(labels)):
        issues.append("distribution labels must be unique")
    if isinstance(record_count, int) and total != record_count:
        issues.append(
            f"distribution counts sum to {total}, expected {record_count}"
        )

    gaps = mapping.get("gaps")
    if not isinstance(gaps, list):
        issues.append("gaps must be a list")
    if "constraints" in mapping:
        issues.extend(validate_material_constraints(
            mapping=mapping, template=template
        ))
    return issues
