"""Publish identifier-safe tool names without changing canonical names.

Canonical names stay ``service.operation``. OpenAI and Anthropic accept only
letters, digits, underscores, and hyphens in a tool name, so every model is
shown the same identifier names: this adapter replaces each ``.`` with ``_``
on the way out and restores the canonical name on the way back. Services,
blueprints, the audit, and the trajectory keep the canonical names.
"""
from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


def uses_identifier_tool_names(model: str) -> bool:
    """Whether this model is shown identifier tool names. True for every model.

    Some APIs require it (OpenAI; Anthropic requires ``^[a-zA-Z0-9_-]{1,128}$``);
    showing every model the same names keeps model comparisons like for like.
    """
    return bool(model)


class ToolNameAdapter:
    """One task's canonical names and the names a restricted model may see."""

    def __init__(self, canonical_names: list[str]):
        self._to_wire: dict[str, str] = {}
        self._to_canonical: dict[str, str] = {}
        for name in canonical_names:
            wire = name.replace(".", "_")
            if not _IDENTIFIER.fullmatch(wire):
                raise ValueError(
                    f"tool name {name!r} is not an identifier after replacing dots"
                )
            previous = self._to_canonical.get(wire)
            if previous is not None and previous != name:
                raise ValueError(
                    f"tool names {previous!r} and {name!r} both publish as {wire!r}"
                )
            self._to_wire[name] = wire
            self._to_canonical[wire] = name

    def publish(self, canonical: str) -> str:
        """Name placed on the model-facing tool list."""
        try:
            return self._to_wire[canonical]
        except KeyError as exc:
            raise KeyError(f"unknown tool name {canonical!r}") from exc

    def canonical(self, published: str) -> str:
        """Canonical name for routing, audit, and the trajectory."""
        return self._to_canonical.get(published, published)

    def adapt_prompt(self, prompt: str) -> str:
        """Rewrite canonical names in a sealed prompt to the published names."""
        rewritten = prompt
        for canonical, wire in sorted(
            self._to_wire.items(), key=lambda item: len(item[0]), reverse=True
        ):
            rewritten = rewritten.replace(canonical, wire)
        return rewritten


def adapter_for_model(model: str, canonical_names: list[str]) -> ToolNameAdapter | None:
    """The identifier adapter for this model, or ``None`` when names stay canonical."""
    if not uses_identifier_tool_names(model):
        return None
    return ToolNameAdapter(canonical_names)
