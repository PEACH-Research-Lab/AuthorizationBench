"""Canonical normalized-verbatim evidence matching."""
from __future__ import annotations

import re


def normalize_evidence(text: object) -> str:
    """Normalize presentation-only differences without allowing paraphrase."""
    value = str(text)
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", value).strip().casefold()


def normalized_verbatim_match(source: object, quote: object) -> bool:
    """Whether quote is a normalized verbatim substring of source."""
    normalized_quote = normalize_evidence(quote)
    return bool(normalized_quote) and normalized_quote in normalize_evidence(
        source
    )
