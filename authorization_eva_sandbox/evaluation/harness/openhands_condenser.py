"""Build an OpenHands 1.49.2 agent and its token condenser.

The harness uses ``openhands-sdk==1.49.2`` from ``.venv-openhands``.
``Agent.system_prompt`` replaces the SDK's built-in prompt. A condenser is
attached only when the task sets ``agent.condenser.max_tokens``.
"""
from __future__ import annotations

from importlib.metadata import version
from typing import Any

SDK_VERSION = "1.49.2"


def require_openhands_sdk() -> None:
    """Refuse any OpenHands SDK other than 1.49.2."""
    installed = version("openhands-sdk")
    if installed != SDK_VERSION:
        raise RuntimeError(
            f"OpenHands harness requires openhands-sdk {SDK_VERSION} "
            f"from .venv-openhands; found {installed}"
        )


def build_openhands_agent(
    task_config: dict,
    agent_llm: Any,
    *,
    system_prompt: str | None,
    condenser_max_tokens: int | None = None,
    **agent_kwargs,
):
    """Construct the 1.49.2 agent with this task's condenser, if any.

    ``system_prompt=None`` keeps the SDK's own ``system_prompt.j2``.
    """
    require_openhands_sdk()
    from openhands.sdk import Agent

    agent_kwargs.pop("condenser", None)
    if system_prompt is not None:
        agent_kwargs["system_prompt"] = system_prompt
    return Agent(
        llm=agent_llm,
        condenser=condenser_from_task(task_config, agent_llm, max_tokens=condenser_max_tokens),
        **agent_kwargs,
    )


def condenser_from_task(task_config: dict, agent_llm: Any, *, max_tokens: int | None = None):
    """Return an SDK condenser, or ``None`` when this task has no token budget.

    ``max_chars`` is ignored. Event-count ``max_size`` stays at the 1.49.2
    default of 240, so a short mailbox is not condensed by message count.
    ``max_tokens`` replaces the task budget; the native prompt condition
    passes ``condenser_budget``'s effective budget here.
    """
    require_openhands_sdk()
    condenser = (task_config.get("agent") or {}).get("condenser") or {}
    budget = condenser.get("max_tokens")
    if not budget:
        return None
    from openhands.sdk.context.condenser.llm_summarizing_condenser import (
        LLMSummarizingCondenser,
    )
    # 0 is valid: nothing at the head is protected. ``or 1`` would turn that into 1.
    raw_keep_first = condenser.get("keep_first")
    keep_first = 0 if raw_keep_first is None else int(raw_keep_first)
    return LLMSummarizingCondenser(
        llm=agent_llm,
        max_tokens=int(max_tokens if max_tokens is not None else budget),
        keep_first=keep_first,
    )
