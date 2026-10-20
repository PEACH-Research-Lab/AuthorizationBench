"""
pricing.py - deterministic, reproducible cost computation from token counts.

We compute agent task-run cost OURSELVES from a fixed price table rather than
trusting the provider-reported `accumulated_cost` for two reasons:
  1. Reproducibility: OpenRouter routes multi-provider models to whichever
     provider is cheapest/available at call time, so its reported cost drifts
     run to run. A fixed table gives a stable number for the paper.
  2. Coverage: some endpoints (self-hosted / brand-new models) report tokens
     but no cost; we still get a number.

Prices are USD per 1,000,000 tokens. The table below is a historical,
date-stamped fallback and is not the source of truth for V4 model selection.
When a model is not in the table, `compute_cost` returns ``priced=False`` and
the caller should fall back to the provider-reported cost.

Billing convention (verified against a real gpt-4.1 run, exact match):
  - `prompt_tokens` is the TOTAL input including the cached portion.
  - `cache_read_tokens` is the cached subset, billed at the cached rate.
  - non-cached input = prompt_tokens - cache_read_tokens, billed at input rate.
  - `completion_tokens` is billed at the output rate; `reasoning_tokens` are a
    SUBSET of completion_tokens (already billed as output) -> never added again.
  - `cache_write_tokens` (Anthropic prompt-cache writes) is billed at the
    write rate when known, else approximated at the input rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# USD per 1,000,000 tokens. `cached` = cache-read rate; `cache_write` optional.
# Keys are normalized model ids (see _normalize). Use the table's headline
# column; alternatives noted inline.
PRICE_TABLE: dict[str, dict[str, float]] = {
    "gpt-5.5": {"input": 5.00, "cached": 0.50, "output": 30.00},
    "gpt-4.1": {"input": 2.00, "cached": 0.50, "output": 8.00},
    # Anthropic: cache read $0.30; 5m write $3.75 / 1h write $6.00 -> use 5m.
    "claude-sonnet-4.6": {"input": 3.00, "cached": 0.30, "output": 15.00,
                          "cache_write": 3.75},
    # Gemini 3.1 Pro: OpenRouter list price column ($2.00 in); weighted-avg
    # input is ~$1.43 if you prefer realized pricing.
    "gemini-3.1-pro": {"input": 2.00, "cached": 0.20, "output": 12.00},
    # Qwen: headline (cheapest, Alibaba Cloud) prices. Weighted-avg alt:
    # thinking ~$0.16/$1.34, instruct ~$0.16/$0.61.
    "qwen3-vl-30b-a3b-thinking": {"input": 0.13, "cached": 0.13, "output": 1.56},
    "qwen3-vl-30b-a3b-instruct": {"input": 0.13, "cached": 0.15, "output": 0.52},
}

# Explicit aliases for slugs that don't normalize cleanly to a table key.
_ALIASES: dict[str, str] = {
    "gemini-3.1-pro-preview": "gemini-3.1-pro",
}


@dataclass
class CostBreakdown:
    model: str
    priced: bool
    cost_usd: float = 0.0
    input_cost: float = 0.0
    cached_cost: float = 0.0
    output_cost: float = 0.0
    cache_write_cost: float = 0.0
    matched_key: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _normalize(model: str) -> str:
    """openrouter/anthropic/claude-sonnet-4.6 -> claude-sonnet-4.6."""
    s = (model or "").strip().lower()
    if s.startswith("openrouter/"):
        s = s[len("openrouter/"):]
    # Drop a leading provider segment (anthropic/, qwen/, google/, openai/...).
    if "/" in s:
        s = s.split("/", 1)[1]
    return s


def lookup_prices(model: str) -> Optional[tuple[str, dict[str, float]]]:
    """Return (matched_key, price_dict) for a model slug, or None."""
    norm = _normalize(model)
    norm = _ALIASES.get(norm, norm)
    if norm in PRICE_TABLE:
        return norm, PRICE_TABLE[norm]
    # Prefix / containment fallback (handles trailing suffixes like -preview).
    for key in PRICE_TABLE:
        if norm.startswith(key) or key in norm:
            return key, PRICE_TABLE[key]
    return None


def compute_cost(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,  # noqa: ARG001 - subset of completion; not billed again
) -> CostBreakdown:
    """
    Deterministic cost from our price table. Returns priced=False (cost 0) when
    the model isn't in the table so the caller can fall back to provider cost.
    """
    found = lookup_prices(model)
    if found is None:
        return CostBreakdown(model=model, priced=False)
    key, p = found

    non_cached_input = max(int(prompt_tokens) - int(cache_read_tokens), 0)
    input_cost = non_cached_input * p["input"] / 1e6
    cached_cost = int(cache_read_tokens) * p.get("cached", p["input"]) / 1e6
    output_cost = int(completion_tokens) * p["output"] / 1e6
    write_rate = p.get("cache_write", p["input"])  # approximate if not listed
    cache_write_cost = int(cache_write_tokens) * write_rate / 1e6

    total = input_cost + cached_cost + output_cost + cache_write_cost
    return CostBreakdown(
        model=model, priced=True, cost_usd=total,
        input_cost=input_cost, cached_cost=cached_cost,
        output_cost=output_cost, cache_write_cost=cache_write_cost,
        matched_key=key,
    )


def compute_cost_from_combined(model: str, combined: dict) -> CostBreakdown:
    """Convenience wrapper over a cost_*.json `combined` block."""
    combined = combined or {}
    return compute_cost(
        model,
        prompt_tokens=combined.get("prompt_tokens", 0) or 0,
        completion_tokens=combined.get("completion_tokens", 0) or 0,
        cache_read_tokens=combined.get("cache_read_tokens", 0) or 0,
        cache_write_tokens=combined.get("cache_write_tokens", 0) or 0,
        reasoning_tokens=combined.get("reasoning_tokens", 0) or 0,
    )
