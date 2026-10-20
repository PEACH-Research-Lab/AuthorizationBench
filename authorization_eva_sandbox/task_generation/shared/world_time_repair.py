"""Localized repair of compiler-owned collection timeplans."""
from __future__ import annotations

import copy
import json
from typing import Any, Callable, Iterable, Mapping

from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    FillEnvelope,
)
from authorization_eva_sandbox.task_generation.shared.world_realization import (
    TIMEPLAN_PRIMARY_LEAVES,
    realization_sha256,
    resolve_relative_time_offset,
    validate_world_realization,
)

try:
    from services.fixture_slots import nested_collection_path, runtime_fixture_template
except ModuleNotFoundError:  # package-mode tests
    from authorization_eva_sandbox.services.fixture_slots import (
        nested_collection_path,
        runtime_fixture_template,
    )


TIMEPLAN_REPAIR_PROMPT = """\
Decide whether this collection-level Stage 2 finding is caused by record
timestamps. If it is not, return exactly:
NOT_TIMEPLAN

If it is, select only existing timepoint locations below and provide sparse
relative-to-NOW anchors. Code will convert them to ISO-8601, interpolate all
records between those anchors,
preserve collection order, and keep compiler-owned timestamp cohorts aligned.
Return one or more blocks and nothing else:
TIMEPLAN_REPAIR
WHERE: <exact allowed location>
OFFSET: <signed offset from NOW, such as -120d, -6h, or +30m>
END

Use the fewest anchors that fully express the temporal requirement. Usually
anchor both sides of a required relation (for example, the old parent and its
recent reply). Never invent a record, field, ID, or location. Do not rewrite
message content. Existing records must normally be at or before NOW unless the
Stage 2 condition explicitly requires future records.

## NOW
{reference_time}

## Stage 2 condition
{condition}

## Finding
PROBLEM: {problem}
SUGGESTION: {suggestion}

## Allowed timepoint locations
{locations}
"""


def _template_for(target: Any, registry: Any) -> Any:
    runtime = runtime_fixture_template(target.template)
    if runtime is not None:
        return runtime
    spec = registry.templates.get(target.template)
    return None if spec is None else spec.fixture_template


def _fixture_collection(
    fixture: Mapping[str, Any], collection_path: str
) -> list[dict[str, Any]]:
    node: Any = fixture
    tokens = collection_path.split(".")
    for index, token in enumerate(tokens):
        last = index == len(tokens) - 1
        is_list = token.endswith("[]")
        key = token[:-2] if is_list else token
        if not isinstance(node, Mapping) or key not in node:
            return []
        node = node[key]
        if last:
            return node if isinstance(node, list) else []
        if is_list:
            if not isinstance(node, list) or not node:
                return []
            node = node[0]
    return []


def _relative(path: str, collection_path: str) -> str:
    prefix = collection_path + "[]."
    return path[len(prefix):] if path.startswith(prefix) else path


def _get_nested(node: Mapping[str, Any], path: str) -> Any:
    current: Any = node
    for token in path.split("."):
        if not isinstance(current, Mapping) or token not in current:
            return None
        current = current[token]
    return current


def _condition_text(envelope: FillEnvelope, finding: Mapping[str, Any]) -> str:
    material = envelope.materials.get(str(finding.get("material_id") or ""))
    if material is None:
        return ""
    index = finding.get("condition_index")
    if isinstance(index, int) and not isinstance(index, bool):
        if 0 <= index < len(material.content_conditions):
            return str(material.content_conditions[index])
    return str(material.instruction_verbatim or "")


def _timepoint_options(
    *,
    realization: Mapping[str, Any],
    envelope: FillEnvelope,
    registry: Any,
    fixtures: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> list[dict[str, Any]]:
    location = finding.get("location")
    address = location.get("address") if isinstance(location, Mapping) else {}
    if not isinstance(address, Mapping):
        return []
    target_id = str(address.get("target_id") or "")
    collection_path = str(address.get("collection_path") or "")
    target = envelope.targets.get(target_id)
    if target is None or not collection_path or target.kind != "service":
        return []
    template = _template_for(target, registry)
    fixture = fixtures.get(target.fixture_document)
    if template is None or not isinstance(fixture, Mapping):
        return []
    candidates = [
        field.path for field in template.fields
        if field.source == "timeplan"
        and nested_collection_path(field.path) == collection_path
        and field.path.rsplit(".", 1)[-1] in TIMEPLAN_PRIMARY_LEAVES
    ]
    if not candidates:
        return []
    field_path = candidates[0]
    compiled_records = _fixture_collection(fixture, collection_path)
    targets = {}
    for group in ("targets", "runtime_targets"):
        block = realization.get(group)
        if isinstance(block, Mapping):
            targets.update(block)
    payload = targets.get(target_id)
    collection = (
        (payload.get("collections") or {}).get(collection_path)
        if isinstance(payload, Mapping) else None
    )
    source_records = (
        collection.get("records") if isinstance(collection, Mapping) else []
    )
    if not isinstance(source_records, list) or len(source_records) != len(
        compiled_records
    ):
        return []
    options: list[dict[str, Any]] = []
    relative = _relative(field_path, collection_path)
    for source, compiled in zip(source_records, compiled_records):
        if not isinstance(source, Mapping) or not isinstance(compiled, Mapping):
            continue
        record_key = str(source.get("record_key") or "")
        if not record_key:
            continue
        values = source.get("values") or {}
        context = {}
        if isinstance(values, Mapping):
            for path, value in values.items():
                leaf = str(path).rsplit(".", 1)[-1]
                if leaf in {"subject", "title", "name", "from", "reply_to"}:
                    context[leaf] = value
        display = (
            f"{target_id} > {collection_path} > {record_key} > {field_path}"
        )
        options.append({
            "display": display,
            "address": {
                "target_id": target_id,
                "collection_path": collection_path,
                "record_key": record_key,
                "field_path": field_path,
            },
            "current_value": _get_nested(compiled, relative),
            "context": context,
        })
    return options


def _parse_timeplan_reply(
    raw: Any,
    options: Iterable[Mapping[str, Any]],
    *,
    reference_time: str,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    option_list = list(options)
    if isinstance(raw, str):
        text = raw.strip()
    elif isinstance(raw, Mapping):
        text = str(raw.get("text") or raw.get("content") or "").strip()
    else:
        text = str(raw or "").strip()
    if text == "NOT_TIMEPLAN":
        return "not_timeplan", [], []
    allowed = {
        str(option["display"]): option
        for option in option_list
    }
    remaining = text
    replacements: list[dict[str, Any]] = []
    seen: set[str] = set()
    while remaining.strip():
        remaining = remaining.lstrip("\n")
        if not remaining.startswith("TIMEPLAN_REPAIR\n"):
            return "error", [], ["timeplan reply must be NOT_TIMEPLAN or repair blocks"]
        remaining = remaining[len("TIMEPLAN_REPAIR\n"):]
        block, marker, tail = remaining.partition("\nEND")
        if not marker:
            return "error", [], ["TIMEPLAN_REPAIR is missing END"]
        lines = [line for line in block.splitlines() if line.strip()]
        if (
            len(lines) != 2
            or not lines[0].startswith("WHERE:")
            or not lines[1].startswith("OFFSET:")
        ):
            return "error", [], ["TIMEPLAN_REPAIR requires WHERE then OFFSET"]
        where = lines[0][len("WHERE:"):].strip()
        offset = lines[1][len("OFFSET:"):].strip().lower()
        if where not in allowed:
            return "error", [], [f"timeplan WHERE is not allowlisted: {where}"]
        if where in seen:
            return "error", [], [f"duplicate timeplan WHERE: {where}"]
        seen.add(where)
        try:
            parsed = resolve_relative_time_offset(offset, reference_time)
        except ValueError as exc:
            return "error", [], [f"timeplan OFFSET {offset!r}: {exc}"]
        value = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
        replacements.append({
            **dict(allowed[where]["address"]),
            "value": value,
            "offset": offset,
        })
        remaining = tail.lstrip("\n")
    if not replacements:
        return "error", [], ["timeplan reply contains no replacements"]
    return "repaired", replacements, []


def findings_for_compile_issues(
    realization: Mapping[str, Any],
    issues: Iterable[str],
) -> list[dict[str, Any]]:
    """Turn a backwards timeplan compile failure into one collection finding."""
    texts = [str(issue) for issue in issues]
    if not any("timeplan anchors" in text for text in texts):
        return []
    problem = next(text for text in texts if "timeplan anchors" in text)
    findings: list[dict[str, Any]] = []
    for group in ("targets", "runtime_targets"):
        block = realization.get(group)
        if not isinstance(block, Mapping):
            continue
        for target_id, payload in block.items():
            collections = payload.get("collections") if isinstance(payload, Mapping) else None
            if not isinstance(collections, Mapping):
                continue
            for collection_path, collection in collections.items():
                records = collection.get("records") if isinstance(collection, Mapping) else None
                if not isinstance(records, list) or not any(
                    isinstance(record, Mapping) and "time_offset" in record
                    for record in records
                ):
                    continue
                findings.append({
                    "material_id": "",
                    "condition_index": None,
                    "problem": problem,
                    "suggestion": (
                        "Keep record order. A later record may share the "
                        "previous timestamp, but must not move earlier."
                    ),
                    "location": {
                        "kind": "collection",
                        "address": {
                            "target_id": str(target_id),
                            "collection_path": str(collection_path),
                        },
                    },
                })
    return findings


def repair_timeplan_findings(
    realization: Mapping[str, Any],
    *,
    findings: Iterable[Mapping[str, Any]],
    envelope: FillEnvelope,
    registry: Any,
    fixtures: Mapping[str, Any] | None,
    reference_time: str,
    repair_call: Callable[[str], Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Localize collection findings onto sparse compiler-owned time anchors."""
    original = copy.deepcopy(dict(realization))
    current = copy.deepcopy(original)
    listed = [dict(value) for value in findings]
    remaining: list[dict[str, Any]] = []
    audit: dict[str, Any] = {
        "attempted": False,
        "applied": False,
        "rolled_back": False,
        "stopped": False,
        "base_sha256": realization_sha256(original),
        "replacements": [],
        "problems": [],
        "requests": [],
    }
    fixture_map = fixtures or {}
    overrides = list(current.get("timeplan_overrides") or [])
    for finding in listed:
        item = dict(finding)
        location = item.get("location")
        if not isinstance(location, Mapping) or location.get("kind") != "collection":
            remaining.append(item)
            continue
        options = _timepoint_options(
            realization=current,
            envelope=envelope,
            registry=registry,
            fixtures=fixture_map,
            finding=item,
        )
        if not options:
            remaining.append(item)
            continue
        audit["attempted"] = True
        prompt = TIMEPLAN_REPAIR_PROMPT.format(
            reference_time=reference_time,
            condition=_condition_text(envelope, item),
            problem=item.get("problem") or "",
            suggestion=item.get("suggestion") or "",
            locations=json.dumps(options, ensure_ascii=False, indent=2),
        )
        raw_reply = repair_call(prompt)
        request_audit = {
            "finding": copy.deepcopy(item),
            "options": copy.deepcopy(options),
            "prompt": prompt,
            "raw_reply": str(raw_reply),
        }
        audit["requests"].append(request_audit)
        status, replacements, problems = _parse_timeplan_reply(
            raw_reply, options, reference_time=reference_time
        )
        if status == "not_timeplan":
            remaining.append(item)
            continue
        if problems:
            audit["stopped"] = True
            audit["rolled_back"] = True
            audit["problems"] = problems
            return original, listed, audit
        request_audit["replacements"] = copy.deepcopy(replacements)
        for replacement in replacements:
            address = {
                key: replacement[key]
                for key in (
                    "target_id", "collection_path", "record_key", "field_path",
                )
            }
            overrides = [
                existing for existing in overrides
                if not all(existing.get(key) == value for key, value in address.items())
            ]
            overrides.append({
                key: replacement[key]
                for key in (
                    "target_id", "collection_path", "record_key",
                    "field_path", "value",
                )
            })
        audit["replacements"].extend(replacements)
    if not audit["replacements"]:
        return original, remaining, audit
    current["timeplan_overrides"] = overrides
    problems = validate_world_realization(
        current, envelope=envelope, registry=registry, prepare=False
    )
    if problems:
        audit["stopped"] = True
        audit["rolled_back"] = True
        audit["problems"] = problems
        return original, listed, audit
    audit["applied"] = True
    audit["result_sha256"] = realization_sha256(current)
    return current, remaining, audit
