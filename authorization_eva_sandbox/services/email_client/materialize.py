"""Values-only content planning and deterministic bulk assembly for Email."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from services._base import validate_fixture
from services.fixture_values import validate_values_plan
from services.fixture_slots import FixtureTemplateSpec


def validate_email_content_plan(
    plan: dict[str, Any],
    *,
    mapping: dict[str, Any],
    template: FixtureTemplateSpec,
) -> list[str]:
    return validate_values_plan(
        plan, mapping=mapping, template=template
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:24] or "email"


def assemble_email_fixture(
    *,
    plan: dict[str, Any],
    mapping: dict[str, Any],
    template: FixtureTemplateSpec,
    user_name: str,
    user_email: str,
    reference_time: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    issues = validate_email_content_plan(
        plan, mapping=mapping, template=template
    )
    if issues:
        raise ValueError("; ".join(issues))

    anchor = datetime.fromisoformat(
        reference_time.replace("Z", "+00:00")
    )
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    categories: dict[str, list[dict[str, Any]]] = {}
    for item in plan["record_prototypes"]:
        label = str(item["distribution"])
        categories.setdefault(label, []).append({
            path.removeprefix("emails[]."): copy.deepcopy(value)
            for path, value in item["values"].items()
        })
    emails: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    ordinal = 0
    for distribution in mapping["distributions"]:
        label = str(distribution["label"])
        count = int(distribution["count"])
        prototypes = categories[label]
        counts[label] = count
        for index in range(count):
            prototype = copy.deepcopy(prototypes[index % len(prototypes)])
            email_id = f"{_slug(label)}-{index + 1:03d}"
            prototype["id"] = email_id
            prototype["timestamp"] = (
                anchor - timedelta(hours=12 * (ordinal + 1))
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
            prototype.setdefault("starred", False)
            prototype.setdefault("priority", "normal")
            prototype.setdefault("attachments", [])
            if count > len(prototypes):
                prototype["body"] = (
                    str(prototype["body"]).rstrip()
                    + f"\n\nMessage reference: {email_id}."
                )
            emails.append(prototype)
            ordinal += 1

    fixture: dict[str, Any] = {
        "user_name": user_name,
        "user_email": user_email,
        "emails": emails,
    }
    fixture.update(copy.deepcopy(plan["scalar_values"]))
    schema_issues = validate_fixture(fixture, template.schema)
    if schema_issues:
        raise ValueError("; ".join(schema_issues))
    return fixture, {
        "record_count": len(emails),
        "distribution_counts": counts,
        "template_schema_valid": True,
    }
