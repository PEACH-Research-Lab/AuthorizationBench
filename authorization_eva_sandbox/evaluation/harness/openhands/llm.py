"""OpenHands LLM construction from an evaluation/config.toml profile."""
from __future__ import annotations

import copy
from typing import Any

def _profile_extra_body(profile: dict[str, Any]) -> dict[str, Any]:
    completion_kwargs = profile.get("completion_kwargs") or {}
    extra_body = completion_kwargs.get("extra_body") if isinstance(completion_kwargs, dict) else None
    return copy.deepcopy(extra_body) if isinstance(extra_body, dict) else {}


def effort_from_profile(profile: dict[str, Any]) -> str | None:
    """The one reasoning effort a profile asks for.

    A profile may state it as ``reasoning_effort`` or, in older profiles, as
    ``extra_body.reasoning.effort``. OpenRouter rejects a request that carries
    both with different values, so two different values are a config error.
    """
    top = profile.get("reasoning_effort")
    reasoning = _profile_extra_body(profile).get("reasoning")
    nested = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if top and nested and str(top) != str(nested):
        raise ValueError(f"profile sets reasoning_effort={top!r} and "
                         f"extra_body.reasoning.effort={nested!r}; keep one")
    effort = top or nested
    return str(effort) if effort else None


def llm_from_profile(profile: dict[str, Any]) -> Any:
    """An SDK LLM from an ``evaluation/config.toml`` profile.

    The effort goes on the SDK field, which sends it in the provider's own
    form (``reasoning_effort`` on Chat Completions, ``reasoning.effort`` on
    Responses), and is removed from ``extra_body`` so the request carries it
    once. Without an effort in the profile the SDK default ("high") applies.
    """
    from openhands.sdk import LLM
    from pydantic import SecretStr

    effort = effort_from_profile(profile)
    extra_body = _profile_extra_body(profile)
    reasoning = extra_body.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        rest = {k: v for k, v in reasoning.items() if k != "effort"}
        if rest:
            extra_body["reasoning"] = rest
        else:
            extra_body.pop("reasoning")
    kwargs: dict[str, Any] = {}
    if effort:
        kwargs["reasoning_effort"] = effort
    if profile.get("max_output_tokens"):
        kwargs["max_output_tokens"] = int(profile["max_output_tokens"])
    return LLM(
        model=str(profile["model"]),
        api_key=SecretStr(str(profile.get("api_key") or "")),
        base_url=profile.get("base_url") or None,
        litellm_extra_body=extra_body,
        **kwargs,
    )

