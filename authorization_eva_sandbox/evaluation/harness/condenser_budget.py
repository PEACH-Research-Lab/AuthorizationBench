"""Condenser budget under the native prompt condition.

Decided 2026-09-24: on a harness that keeps its own system prompt, the
condenser budget is the task's ``agent.condenser.max_tokens`` plus the token
count of that native prompt, so condensation fires at the same point in the
conversation as under the sealed prompt. Tokens are counted as the condensers
count them: litellm ``token_counter`` on the agent model string. The sealed
condition keeps the task budget and records nothing new.
"""
from __future__ import annotations

from typing import Any, Callable

from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
    NATIVE,
    check_condition,
)

TOKEN_COUNTER = "litellm.token_counter(model=<agent model>, text=<native prompt>)"


def task_max_tokens(task_config: dict[str, Any]) -> int | None:
    agent = task_config.get("agent") if isinstance(task_config, dict) else None
    raw = agent.get("condenser") if isinstance(agent, dict) else None
    if not isinstance(raw, dict) or not raw.get("max_tokens"):
        return None
    return int(raw["max_tokens"])


def native_prompt_tokens(
    native_text: str,
    model: str,
    *,
    count: Callable[[str, str], int] | None = None,
) -> int:
    if not native_text:
        return 0
    if count is not None:
        return int(count(model, native_text))
    import litellm

    return int(litellm.token_counter(model=model, text=native_text))


def condenser_budget(
    task_config: dict[str, Any],
    *,
    condition: str,
    native_text: str,
    model: str,
    count: Callable[[str, str], int] | None = None,
) -> dict[str, Any] | None:
    """The three numbers for a native run of a task with a token budget, else None."""
    check_condition(condition)
    task_budget = task_max_tokens(task_config)
    if task_budget is None or condition != NATIVE:
        return None
    native_tokens = native_prompt_tokens(native_text, model, count=count)
    return {
        "task_max_tokens": task_budget,
        "native_prompt_tokens": native_tokens,
        "effective_max_tokens": task_budget + native_tokens,
        "model": model,
        "token_counter": TOKEN_COUNTER,
    }
