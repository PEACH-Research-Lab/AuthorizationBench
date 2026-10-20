"""World grounding for the judges (reader side of Stage 4 M4's grounding table).

The skeleton prose the judges receive (`completion_criteria`,
`violated_actions`) was written before the world existed. Stage 4 M4 compiles
`<bundle>/_generation/grounding.json` once the world is frozen: for each
item a `realization_note` says how THIS sandbox realizes the item's
references and values ("the microwave" = product a900-microwave; "$200" =
live price $199.99; the sale window is 18:00-21:00Z). The prose itself is
never edited — the judge sees prose + note side by side.

Notes are matched to items by verbatim prose text (bindings carry the
skeleton text verbatim), so a stale/absent grounding simply yields no note.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def load_grounding(bundle_dir: Optional[Path]) -> Optional[dict]:
    if bundle_dir is None:
        return None
    path = Path(bundle_dir) / "_generation" / "grounding.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _norm(text: str) -> str:
    return " ".join(str(text).split()).lower()


def realization_notes(bundle_dir: Optional[Path], kind: str,
                      items: list[str]) -> dict[str, str]:
    """{item_text: realization_note} for the given kind
    ('completion_criterion' | 'violated_action'), matched by prose text."""
    g = load_grounding(bundle_dir)
    if not g:
        return {}
    by_prose = {_norm(r.get("prose", "")): str(r.get("realization_note", ""))
                for r in g.get("realizations") or []
                if isinstance(r, dict) and r.get("kind") == kind and r.get("realization_note")}
    out: dict[str, str] = {}
    for item in items:
        note = by_prose.get(_norm(item))
        if note and not note.strip().upper().startswith("MISMATCH:"):
            out[item] = note
    return out


def references_block(bundle_dir: Optional[Path]) -> str:
    """Deictic references resolved for this world, as a short markdown list
    (empty string when there is no grounding)."""
    g = load_grounding(bundle_dir)
    refs = (g or {}).get("references") or []
    if not refs:
        return ""
    lines = ["- " + f"\"{r.get('mention', '')}\" → {r.get('resolves_to', '')}"
             for r in refs if isinstance(r, dict)]
    return "\n".join(lines)
