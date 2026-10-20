"""LLM multiple-choice field identity, then mechanical path rewrite."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    FillEnvelope,
)

try:
    from services.fixture_slots import nested_collection_path
except ModuleNotFoundError:
    from authorization_eva_sandbox.services.fixture_slots import nested_collection_path


ClassifyCall = Callable[[str], Any]
PREVIEW_LIMIT = 220
SKIP_PATH = "SKIP"
ANSWER_LINE = re.compile(
    r"(?m)^\s*#?\s*(\d+)\s*[:.)=\]]?\s*([A-Za-z])\b"
)


def coerce_answer_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("answers", "text", "content"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return "\n".join(str(item) for item in value)
        lines: list[str] = []
        for key, value in raw.items():
            if str(key).isdigit() and isinstance(value, str) and value.strip():
                lines.append(f"{key}{value.strip()[0]}")
        if lines:
            return "\n".join(lines)
    if isinstance(raw, list):
        return "\n".join(str(item) for item in raw)
    return str(raw or "")


def _is_record_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"$record_ref"}


def _preview(value: Any) -> str:
    if _is_record_ref(value):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return value[:PREVIEW_LIMIT]
    text = json.dumps(value, ensure_ascii=False)
    return text[:PREVIEW_LIMIT]


def _letters(count: int) -> list[str]:
    return [chr(ord("A") + index) for index in range(count)]


@dataclass
class MenuOption:
    letter: str
    path: str
    meaning: str
    format: str = ""
    source: str = ""


@dataclass
class ClassificationItem:
    number: int
    kind: str
    authored: str
    place: str
    preview: str
    menu: list[MenuOption]
    holder: dict[str, Any]
    dest: dict[str, Any] | None = None
    ref: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    collection_map: dict[str, Any] | None = None
    notes: dict[str, Any] = field(default_factory=dict)


def _field_option(template: Any, path: str, letter: str) -> MenuOption:
    spec = template.field(path) if template is not None else None
    if spec is None:
        return MenuOption(letter=letter, path=path, meaning=path)
    return MenuOption(
        letter=letter,
        path=path,
        meaning=spec.meaning or path,
        format=spec.format,
        source=spec.source,
    )


def _skip_option(letter: str) -> MenuOption:
    return MenuOption(
        letter=letter,
        path=SKIP_PATH,
        meaning="None of these closed fields",
        source="skip",
    )


def _menu_for_paths(template: Any, paths: list[str]) -> list[MenuOption]:
    letters = _letters(len(paths) + 1)
    options = [
        _field_option(template, path, letters[index])
        for index, path in enumerate(paths)
    ]
    options.append(_skip_option(letters[len(paths)]))
    return options


def build_classification_items(
    realization: dict[str, Any],
    envelope: FillEnvelope,
    registry: Any,
) -> list[ClassificationItem]:
    from authorization_eva_sandbox.task_generation.shared.world_realization import (
        _allowed_model_fields,
        _collection_allowed,
        _forbidden_fields,
        _target_template,
    )

    items: list[ClassificationItem] = []
    number = 1

    def add(item: ClassificationItem) -> None:
        nonlocal number
        item.number = number
        items.append(item)
        number += 1

    for group in ("targets", "runtime_targets"):
        block = realization.get(group)
        if not isinstance(block, dict):
            continue
        for target_id, payload in block.items():
            if target_id not in envelope.targets or not isinstance(payload, dict):
                continue
            target = envelope.targets[target_id]
            template = _target_template(target.template, registry)
            allowed = _allowed_model_fields(envelope, target_id)
            model_scalars = sorted(
                path for path in allowed if not nested_collection_path(path)
            )
            if target.template in {"runtime:user_chat", "user_chat"}:
                if "instruction" not in model_scalars:
                    model_scalars.append("instruction")
            collections = payload.get("collections")
            if not isinstance(collections, dict):
                collections = {}
            collection_allowed = sorted({
                nested_collection_path(path)
                for path in allowed if nested_collection_path(path)
            })
            for authored, collection in list(collections.items()):
                if authored in collection_allowed:
                    continue
                add(ClassificationItem(
                    number=0,
                    kind="collection_key",
                    authored=str(authored),
                    place=f"collection on {target_id}",
                    preview=str(authored),
                    menu=_menu_for_paths(template, collection_allowed),
                    holder=collections,
                    collection_map=collections,
                    payload=payload,
                ))
            unbound_scalars = payload.get("unbound_values")
            if isinstance(unbound_scalars, dict) and model_scalars:
                scalars = payload.setdefault("scalar_values", {})
                for authored, value in list(unbound_scalars.items()):
                    add(ClassificationItem(
                        number=0,
                        kind="value_key",
                        authored=str(authored),
                        place=f"scalar fields on {target_id}",
                        preview=_preview(value),
                        menu=_menu_for_paths(template, model_scalars),
                        holder=unbound_scalars,
                        dest=scalars,
                    ))
            for collection_path, collection in list(collections.items()):
                if not isinstance(collection, dict):
                    continue
                if collection_path in collection_allowed:
                    model_fields = sorted(
                        path for path in allowed
                        if nested_collection_path(path) == collection_path
                    )
                else:
                    model_fields = sorted(
                        path for path in allowed if nested_collection_path(path)
                    )
                records = collection.get("records")
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    record_key = str(record.get("record_key") or "")
                    place = (
                        f"{target.template} record {record_key} in "
                        f"{collection_path}"
                    )
                    values = record.setdefault("values", {})
                    unbound = record.get("unbound_values")
                    if isinstance(unbound, dict) and model_fields:
                        for authored, value in list(unbound.items()):
                            add(ClassificationItem(
                                number=0,
                                kind="value_key",
                                authored=str(authored),
                                place=place,
                                preview=_preview(value),
                                menu=_menu_for_paths(template, model_fields),
                                holder=unbound,
                                dest=values,
                            ))
                    for value in list(values.values()) + list(
                        (unbound or {}).values() if isinstance(unbound, dict) else []
                    ):
                        if not _is_record_ref(value):
                            continue
                        ref = value.get("$record_ref")
                        if not isinstance(ref, dict):
                            continue
                        field_path = str(ref.get("field_path") or "")
                        ref_target = str(ref.get("target_id") or "")
                        ref_collection = str(ref.get("collection_path") or "")
                        if ref_target not in envelope.targets:
                            continue
                        ref_template = _target_template(
                            envelope.targets[ref_target].template, registry
                        )
                        ref_forbidden = _forbidden_fields(envelope, ref_target)
                        ref_collections = sorted(
                            _collection_allowed(envelope, ref_target)
                        )
                        if (
                            ref_collection
                            and ref_collections
                            and ref_collection not in ref_collections
                        ):
                            add(ClassificationItem(
                                number=0,
                                kind="record_ref_collection",
                                authored=ref_collection,
                                place=(
                                    f"{place}: pointer collection on "
                                    f"{ref_target}"
                                ),
                                preview=ref_collection,
                                menu=_menu_for_paths(
                                    ref_template, ref_collections
                                ),
                                holder=ref,
                                ref=ref,
                            ))
                        if ref_collection in ref_collections:
                            stable = sorted(
                                path for path in ref_forbidden
                                if nested_collection_path(path) == ref_collection
                                and ref_template.field(path) is not None
                                and ref_template.field(path).format == "stable_id"
                                and ref_template.field(path).source == "compiler"
                            )
                        else:
                            stable = sorted(
                                path for path in ref_forbidden
                                if nested_collection_path(path)
                                and ref_template.field(path) is not None
                                and ref_template.field(path).format == "stable_id"
                                and ref_template.field(path).source == "compiler"
                            )
                        if not stable or field_path in stable:
                            continue
                        add(ClassificationItem(
                            number=0,
                            kind="record_ref_field",
                            authored=field_path,
                            place=(
                                f"{place}: pointer to "
                                f"{ref_target}.{ref_collection}"
                            ),
                            preview=_preview(value),
                            menu=_menu_for_paths(ref_template, stable),
                            holder=ref,
                            ref=ref,
                        ))
    return items


def render_classification_prompt(items: list[ClassificationItem]) -> str:
    blocks = [
        "Assign each authored value to ONE closed field card.",
        "Each option lists the compiler path plus its meaning.",
        "Reply with one answer per line, like:",
        "1A",
        "2C",
        "You may also write 1: A or 1.A. Do not invent paths.",
        "Pick skip when none of the cards fit.",
        "",
    ]
    for item in items:
        lines = [
            f"#{item.number}",
            f"place: {item.place}",
            f"label: {item.authored}",
            f"value: {item.preview}",
            "options:",
        ]
        for option in item.menu:
            extra = ", ".join(
                part for part in (option.format, option.source) if part
            )
            suffix = f" [{extra}]" if extra else ""
            lines.append(
                f"{option.letter}  {option.path} — {option.meaning}{suffix}"
            )
        blocks.append("\n".join(lines))
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def parse_answer_key(
    text: str, items: list[ClassificationItem]
) -> tuple[dict[int, str], list[int]]:
    valid = {item.number: {opt.letter for opt in item.menu} for item in items}
    answers: dict[int, str] = {}
    for match in ANSWER_LINE.finditer(text or ""):
        number = int(match.group(1))
        letter = match.group(2).upper()
        if number not in valid or letter not in valid[number]:
            continue
        if number not in answers:
            answers[number] = letter
    unparsed = [item.number for item in items if item.number not in answers]
    return answers, unparsed


def render_cleanup_prompt(
    items: list[ClassificationItem],
    raw_text: str,
    unparsed: list[int],
) -> str:
    valid_lines = []
    lookup = {item.number: item for item in items}
    for number in unparsed:
        item = lookup[number]
        letters = ",".join(opt.letter for opt in item.menu)
        valid_lines.append(f"#{number} allowed={letters}")
    return (
        "Rewrite the answer key as lines like 1A.\n"
        "Only these unanswered item numbers are needed:\n"
        + "\n".join(valid_lines)
        + "\n\nOriginal reply:\n"
        + (raw_text or "")
    )


def apply_classification(
    items: list[ClassificationItem],
    answers: Mapping[int, str],
    audit: dict[str, Any],
) -> None:
    audit.setdefault("bound", [])
    audit.setdefault("unbound", [])
    audit.setdefault("classified", [])
    for item in items:
        letter = answers.get(item.number)
        option = next((opt for opt in item.menu if opt.letter == letter), None)
        if option is None or option.path == SKIP_PATH:
            audit["unbound"].append({
                "where": item.place,
                "from": item.authored,
                "reason": "skip" if option is not None else "unanswered",
            })
            continue
        path = option.path
        if item.kind == "value_key":
            if item.authored not in item.holder:
                audit["unbound"].append({
                    "where": item.place,
                    "from": item.authored,
                    "reason": "missing_holder",
                })
                continue
            if not isinstance(item.dest, dict):
                continue
            if path in item.dest:
                audit["unbound"].append({
                    "where": item.place,
                    "from": item.authored,
                    "reason": "duplicate_bind",
                })
                continue
            item.dest[path] = item.holder.pop(item.authored)
        elif item.kind == "record_ref_field" and item.ref is not None:
            item.ref["field_path"] = path
        elif item.kind == "record_ref_collection" and item.ref is not None:
            item.ref["collection_path"] = path
        elif item.kind == "collection_key" and item.collection_map is not None:
            if path in item.collection_map:
                audit["unbound"].append({
                    "where": item.place,
                    "from": item.authored,
                    "reason": "duplicate_bind",
                })
                continue
            item.collection_map[path] = item.collection_map.pop(item.authored)
        else:
            continue
        audit["bound"].append({
            "where": item.place,
            "from": item.authored,
            "to": path,
            "rule": "classified",
        })
        audit["classified"].append({
            "number": item.number,
            "from": item.authored,
            "to": path,
            "letter": letter,
        })


def classify_world_fields(
    realization: dict[str, Any],
    envelope: FillEnvelope,
    registry: Any,
    *,
    classify_call: ClassifyCall,
    cleanup_call: ClassifyCall | None = None,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit = audit if audit is not None else {
        "skeleton": [], "bound": [], "unbound": [], "classified": [],
    }
    items = build_classification_items(realization, envelope, registry)
    if not items:
        audit["classification"] = {"items": 0, "answered": 0, "unparsed": []}
        return audit
    prompt = render_classification_prompt(items)
    raw = classify_call(prompt)
    text = coerce_answer_text(raw)
    answers, unparsed = parse_answer_key(text, items)
    cleanup_text = ""
    if unparsed and cleanup_call is not None:
        cleanup_text = coerce_answer_text(
            cleanup_call(render_cleanup_prompt(items, text, unparsed))
        )
        extra, unparsed = parse_answer_key(cleanup_text, items)
        for number, letter in extra.items():
            answers.setdefault(number, letter)
    apply_classification(items, answers, audit)
    audit["classification"] = {
        "items": len(items),
        "answered": len(answers),
        "unparsed": unparsed,
        "prompt": prompt,
        "raw": text,
        "cleanup": cleanup_text,
    }
    return audit
