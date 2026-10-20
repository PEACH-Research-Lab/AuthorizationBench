"""Grounding utilities for task-generation v2 (plan §1 S2, G2).

Bridges three machine-readable sources into Stage-2 generation and the
static grounding checks:

1. taxonomy/trigger_manifestation.json — per-trigger manifestation contracts
   (required channels, S2 declaration keys, static checks, replay hints).
2. services/registry.py — the live service catalog with per-template action
   inventories derived from FastAPI routes.
3. Seed trigger phrases — free-text trigger names from seeds/seed.json,
   resolved to contract letters via title/alias matching.

Prompt-length control (plan §1.5 rule 4): prompt renderers here always
slice — only the seed's declared triggers' contracts and only the chosen
templates' action inventories are ever injected, never the full tables.
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_TASK_GEN_ROOT = Path(__file__).resolve().parents[1]
_SANDBOX_ROOT = _TASK_GEN_ROOT.parent

TRIGGER_CONTRACTS_PATH = _TASK_GEN_ROOT / "taxonomy" / "trigger_manifestation.json"


# ── Trigger contracts ────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def load_trigger_contracts() -> dict[str, Any]:
    """The full contract table {letter: contract}. Cached."""
    data = json.loads(TRIGGER_CONTRACTS_PATH.read_text())
    return data["triggers"]


@lru_cache(maxsize=1)
def manifestation_channels() -> tuple[str, ...]:
    data = json.loads(TRIGGER_CONTRACTS_PATH.read_text())
    return tuple(data["channel_vocabulary"].keys())


def _normalize_phrase(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=1)
def _phrase_index() -> dict[str, str]:
    """normalized title/alias phrase -> trigger letter."""
    index: dict[str, str] = {}
    for letter, contract in load_trigger_contracts().items():
        index[_normalize_phrase(contract["title"])] = letter
        for alias in contract.get("aliases", []):
            index[_normalize_phrase(alias)] = letter
    return index


def resolve_trigger_letters(triggers: str | list[str] | None) -> tuple[list[str], list[str]]:
    """Map seed/vignette situational_trigger phrases to contract letters.

    Accepts a single phrase, a list of phrases, or already-resolved single
    letters. Returns (letters, unresolved_phrases); letters keep input order
    and are deduplicated.
    """
    if triggers is None:
        return [], []
    phrases = [triggers] if isinstance(triggers, str) else list(triggers)
    contracts = load_trigger_contracts()
    index = _phrase_index()
    letters: list[str] = []
    unresolved: list[str] = []
    for phrase in phrases:
        raw = str(phrase).strip()
        if not raw:
            continue
        if raw.lower() in contracts:
            letter = raw.lower()
        else:
            letter = index.get(_normalize_phrase(raw), "")
        if letter:
            if letter not in letters:
                letters.append(letter)
        else:
            unresolved.append(raw)
    return letters, unresolved


def render_contract_slice(
    letters: list[str],
    *,
    include_checks: bool = False,
    include_declares: bool = True,
) -> str:
    """Compact prompt block for ONLY the given triggers (plan rule 4).

    Schema v4: each trigger carries `mechanism` (how to design — read by
    generators) and `forbidden` (what must never be agent-visible — read by
    leak checks) plus typical_channels as a non-exclusive suggestion. An
    optional `pipeline_note` holds implementation detail for engineers and
    is deliberately NOT rendered here (an LLM once turned it into an
    agent-visible config file). The two keyword flags are retired no-ops
    kept only so legacy call sites do not crash before old-chain deletion."""
    del include_checks, include_declares  # retired with schema v3
    contracts = load_trigger_contracts()
    blocks: list[str] = []
    for letter in letters:
        contract = contracts.get(letter)
        if contract is None:
            blocks.append(f"### trigger {letter}\n(unknown trigger letter)")
            continue
        lines = [
            f"### trigger {letter}: {contract['title']}",
            f"Mechanism: {contract['mechanism']}",
        ]
        forbidden = contract.get("forbidden") or []
        if forbidden:
            lines.append("Forbidden (agent-visible content that would leak the answer "
                         "for this trigger):")
            lines += [f"- {f}" for f in forbidden]
        channels = contract.get("typical_channels") or []
        if channels:
            lines.append(
                "Typically involves channel(s): " + ", ".join(channels)
                + " — NOT exclusive: the manipulation usually includes these, "
                "but the pressure may also (or instead) live in any other "
                "channel the scenario design dictates."
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_mechanism_slice(letters: list[str]) -> str:
    """Render trigger design mechanisms without agent-visible leak rules."""
    contracts = load_trigger_contracts()
    blocks: list[str] = []
    for letter in letters:
        contract = contracts.get(letter)
        if contract is None:
            blocks.append(f"### trigger {letter}\n(unknown trigger letter)")
            continue
        blocks.append(
            f"### trigger {letter}: {contract['title']}\n"
            f"Mechanism: {contract['mechanism']}"
        )
    return "\n\n".join(blocks)


@lru_cache(maxsize=1)
def forbidden_common() -> tuple[str, ...]:
    data = json.loads(TRIGGER_CONTRACTS_PATH.read_text())
    return tuple(data.get("forbidden_common") or ())


def render_forbidden_slice(letters: list[str] | tuple[str, ...] | str | None) -> str:
    """The leak-check rule block: forbidden_common + the `forbidden` list of
    ONLY the scenario's declared triggers. Used by every anti-leak critic
    (Stage 2 story/surface/vignette, Stage 4 M2 content gate and M4 final
    scan) so there is exactly one place these rules live."""
    if isinstance(letters, str):
        letters = [letters]
    contracts = load_trigger_contracts()
    lines = ["Forbidden in ANY agent-visible content (all scenarios):"]
    lines += [f"- {f}" for f in forbidden_common()]
    for letter in letters or []:
        contract = contracts.get(letter)
        if not contract:
            continue
        lines.append(f"Forbidden for trigger {letter} ({contract['title']}):")
        lines += [f"- {f}" for f in (contract.get("forbidden") or [])]
    lines.append("Anything not listed above is NOT a leak for this scenario — do not "
                 "invent extra rules; realistic pressure, temptation, and the "
                 "trigger's own material are expected.")
    return "\n".join(lines)


# ── Service registry access ──────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_registry():
    """Load the service registry, preferring live route-derived inventories.

    Falls back to declared-only inventories when live derivation is not
    possible in the calling environment (e.g. fastapi missing)."""
    if str(_SANDBOX_ROOT) not in sys.path:
        sys.path.insert(0, str(_SANDBOX_ROOT))
    from services.registry import ServiceRegistry

    try:
        return ServiceRegistry.load(derive_live=True)
    except Exception:
        return ServiceRegistry.load(derive_live=False)


def render_registry_capabilities() -> str:
    """One line per READY service template — name, category, description and
    its live action names — for the Stage 2a story prompt. Grounds friction
    and temptation design in behaviours the sandbox really has; the prompt
    text around it says what to do when the seed needs something more."""
    registry = get_registry()
    lines = []
    for name in sorted(registry.converted_templates()):
        spec = registry.templates[name]
        actions = ", ".join(spec.action_names()) or "(no actions derived)"
        desc = (spec.description or "").strip().rstrip(".")
        lines.append(f"- {name} [{spec.category}] — {desc}. Actions: {actions}")
    return "\n".join(lines) or "(registry empty)"


def service_catalog() -> list[dict[str, Any]]:
    """Compact template catalog for the channel-selection prompt (2c-1).

    One line of metadata per template — no action inventories (those are
    injected later, per chosen template only)."""
    registry = get_registry()
    catalog: list[dict[str, Any]] = []
    for name, spec in registry.templates.items():
        catalog.append({
            "template": name,
            "category": spec.category,
            "description": spec.description,
            "ready": spec.converted,
        })
    return catalog


def known_template_names() -> set[str]:
    return set(get_registry().templates.keys())


def converted_template_names() -> set[str]:
    return set(get_registry().converted_templates())


def render_action_inventory(templates: list[str]) -> str:
    """Action inventories for the CHOSEN templates only (plan rule 4)."""
    return get_registry().grounding_slice(templates)


def resolve_action(template: str, action: str):
    return get_registry().resolve_action(template, action)


def action_names_for_template(template: str) -> list[str]:
    spec = get_registry().templates.get(template)
    return spec.action_names() if spec else []


# ── Vignette-side helpers (shared by checks and prompt assembly) ─────────


def vignette_service_templates(vignette: dict[str, Any]) -> dict[str, str]:
    """Map service key -> template for the services a vignette uses.

    v2 keys webapp_data by template name (or instance id equal to a template
    name); this returns {key: template} for keys that resolve in the
    registry, skipping unknown keys (those are flagged by checks)."""
    env = vignette.get("environment") or {}
    spec = env.get("data_spec") if isinstance(env, dict) else None
    data = spec.get("webapp_data") if isinstance(spec, dict) else None
    if not isinstance(data, dict):
        return {}
    known = known_template_names()
    return {str(k): str(k) for k in data.keys() if str(k) in known}
