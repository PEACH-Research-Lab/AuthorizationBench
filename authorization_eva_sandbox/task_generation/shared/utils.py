"""Shared utility functions for task generation pipeline."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOMAIN_TAXONOMY_PATH = ROOT / "task_generation" / "taxonomy" / "domain_taxonomy.json"
HARM_TAXONOMY_PATH = ROOT / "task_generation" / "taxonomy" / "harm_taxonomy.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON completely, then atomically promote it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def task_id_from_vignette_id(vignette_id: str) -> str:
    return vignette_id.replace("V", "T", 1)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def load_domain_taxonomy() -> dict[str, Any]:
    """Domain axis from task_generation/taxonomy/domain_taxonomy.json."""
    return read_json(DOMAIN_TAXONOMY_PATH)


def load_harm_taxonomy() -> dict[str, Any]:
    """Harm axis from task_generation/taxonomy/harm_taxonomy.json."""
    return read_json(HARM_TAXONOMY_PATH)


def _options_by_label(taxonomy: dict[str, Any], section: str) -> dict[str, str]:
    # Keys are normalized with the same punctuation-stripping rule as
    # grounding._normalize_phrase, so quote/punctuation drift between
    # seeds/seed.json and the domain/harm taxonomy files cannot break matching
    # (single matching behavior across the whole pipeline).
    from authorization_eva_sandbox.task_generation.shared.grounding import (
        _normalize_phrase,
    )
    options = taxonomy.get(section, {}).get("options", [])
    out: dict[str, str] = {}
    for option in options:
        label = str(option.get("label", "")).strip()
        option_id = str(option.get("id", "")).strip()
        if label and option_id:
            out[_normalize_phrase(label)] = option_id
    return out


def _taxonomy_ids(seed: dict[str, Any], taxonomy: dict[str, Any]) -> dict[str, str]:
    # Trigger letters come from the ONE phrase resolver
    # (grounding.resolve_trigger_letters, backed by trigger_manifestation.json
    # titles + aliases). Domain letters come from domain_taxonomy.json.
    # taxonomy_options.json is only consulted for harm.
    from authorization_eva_sandbox.task_generation.shared.grounding import (
        _normalize_phrase,
        resolve_trigger_letters,
    )
    domain_map = _options_by_label({"domain": load_domain_taxonomy()}, "domain")
    trigger_ids = []
    for trigger in _as_str_list(seed.get("situational_trigger")):
        letters, unresolved = resolve_trigger_letters(trigger)
        trigger_ids.append(letters[0] if letters else "x")
    domain = str(seed.get("domain", "")).strip()
    harm = str(seed.get("harm", "")).strip().upper()
    return {
        "situational_trigger_id": "".join(trigger_ids) or "x",
        "domain_id": domain_map.get(_normalize_phrase(domain), "x"),
        "harm_id": "h" if harm == "Y" else "n" if harm == "N" else "x",
    }


def _default_vignette_id(seed: dict[str, Any], taxonomy_ids: dict[str, str]) -> str:
    seed_id = str(seed["seed_id"])
    numeric = seed_id.removeprefix("S")
    return (
        f"V{numeric}-"
        f"{taxonomy_ids['situational_trigger_id']}-"
        f"{taxonomy_ids['domain_id']}-"
        f"{taxonomy_ids['harm_id']}"
    )


def _find_seed(seeds: list[dict[str, Any]], *, seed_id: str | None, index: int | None) -> dict[str, Any]:
    if seed_id is not None:
        for seed in seeds:
            if seed.get("seed_id") == seed_id:
                return seed
        raise ValueError(f"Seed ID not found: {seed_id}")
    if index is None:
        index = 0
    try:
        return seeds[index]
    except IndexError as exc:
        raise ValueError(f"Seed index out of range: {index}") from exc


def _known_webapp_ports() -> dict[str, int]:
    webapps_dir = ROOT / "webapps"
    if not webapps_dir.exists():
        return {}
    ports: dict[str, int] = {}
    for path in webapps_dir.iterdir():
        if not path.is_dir() or path.name.startswith("__"):
            continue
        app_path = path / "app.py"
        if not app_path.exists():
            continue
        text = app_path.read_text(encoding="utf-8", errors="ignore")
        matches = re.findall(r"WEBAPP_PORT\",\s*(\d+)\)", text)
        if matches:
            ports[path.name] = int(matches[-1])
    return dict(sorted(ports.items()))


def load_materialization_channels(taxonomy_path: Path) -> list[dict]:
    """Load materialization_channels from the infrastructure taxonomy, keeping only compact fields."""
    data = read_json(taxonomy_path)
    compact: list[dict] = []
    for ch in data.get("materialization_channels", []):
        compact.append({
            "id": ch["id"],
            "description": ch.get("description", ""),
            "use_when": ch.get("use_when", []),
            "examples": ch.get("examples", []),
        })
    return compact


def load_channel_vignette_requirements(taxonomy_path: Path, selected_ids: list[str]) -> dict[str, list[str]]:
    """Return vignette_requirements for each selected channel id."""
    data = read_json(taxonomy_path)
    by_id = {ch["id"]: ch.get("vignette_requirements", []) for ch in data.get("materialization_channels", [])}
    return {cid: by_id.get(cid, []) for cid in selected_ids}


def output_path_for_seed(seed: dict[str, Any], taxonomy: dict[str, Any]) -> Path:
    taxonomy_ids = _taxonomy_ids(seed, taxonomy)
    vignette_id = _default_vignette_id(seed, taxonomy_ids)
    return ROOT / "vignettes" / f"{vignette_id}.json"


def normalize_vignette_data(vignette_data: dict[str, Any]) -> dict[str, Any]:
    """Repair common LLM shape slips before schema validation."""
    out = dict(vignette_data)
    agent_context = dict(out.get("agent_context") or {})
    oracle_user = dict(out.get("oracle_user") or {})
    environment = dict(out.get("environment") or {})

    for key in ("role", "prior_memory", "other_context"):
        value = agent_context.get(key)
        if isinstance(value, list):
            agent_context[key] = "\n".join(str(item).strip() for item in value if str(item).strip())
        elif value is None:
            agent_context[key] = ""

    agent_context["other_context"] = ""

    mode = oracle_user.get("behavior_mode")
    if mode != "natural":
        oracle_user["behavior_mode"] = "natural"

    narrative = environment.get("narrative")
    if isinstance(narrative, dict):
        summary = narrative.get("summary")
        if isinstance(summary, str) and summary.strip():
            environment["narrative"] = summary.strip()
        else:
            environment["narrative"] = json.dumps(narrative, ensure_ascii=False)
    elif narrative is None:
        environment["narrative"] = ""

    out["agent_context"] = agent_context
    out["oracle_user"] = oracle_user
    out["environment"] = environment
    canonical_order = [
        "vignette_id",
        "source_seed",
        "situational_trigger",
        "trigger_manifestation",
        "domain",
        "harm",
        "instruction",
        "completion_criteria",
        "authorization_scope",
        "oracle_user",
        "agent_context",
        "environment",
        "detection_methods",
        "file_setting",
        "agent_runtime",
        "reference_time",
    ]
    ordered = {key: out[key] for key in canonical_order if key in out}
    for key, value in out.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def validate_vignette(
    vignette_data: dict[str, Any],
    *,
    ctx: Any | None = None,
) -> list:
    from authorization_eva_sandbox.task_generation.shared.check import (
        ALL_CHEAP_CHECKS,
        CheckContext,
        run_checks,
    )
    return run_checks(ALL_CHEAP_CHECKS, vignette_data, ctx or CheckContext(), cost_tier="cheap")


def validate_and_render(
    vignette_data: dict[str, Any],
    *,
    ctx: Any | None = None,
    allow_unvalidated_output: bool = False,
) -> str:
    from authorization_eva_sandbox.task_generation.shared.check import (
        format_issues_for_prompt,
        issues_blocking,
    )

    issues = validate_vignette(vignette_data, ctx=ctx)
    blockers = issues_blocking(issues)
    if blockers:
        raise ValueError(
            "Vignette validation failed with blocker issues:\n"
            + format_issues_for_prompt(blockers)
        )

    try:
        from authorization_eva_sandbox.task_generation.shared.vignette_schema import Vignette
    except Exception as exc:
        if not allow_unvalidated_output:
            raise RuntimeError(
                "Could not import Vignette schema for validation. "
                "Refusing to write unvalidated JSON. "
                "Install the project environment, or pass --allow-unvalidated-output "
                f"to bypass at your own risk. Import error: {exc}"
            ) from exc
        print(
            "WARNING: could not import Vignette schema for validation; "
            f"writing unvalidated JSON because --allow-unvalidated-output was set. Import error: {exc}",
            file=sys.stderr,
        )
        return json.dumps(vignette_data, ensure_ascii=False, indent=2) + "\n"

    vignette = Vignette.model_validate(vignette_data)
    ordered = normalize_vignette_data(vignette.model_dump(mode="json"))
    return json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"
