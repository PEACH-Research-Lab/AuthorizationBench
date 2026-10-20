"""OpenHands 1.49.2 conversation loop. SDK imports stay inside functions."""
from __future__ import annotations

import inspect
import threading
from typing import Any, Callable

# June's --task-timeout-seconds default. The 1.49.2 client has no SIGINT
# handler that returns the in-progress conversation, so the runner pauses
# the conversation and still writes the run files.
DEFAULT_TASK_TIMEOUT_SECONDS = 300.0
# Plain agent replies answered by the Oracle in one run; the step cap and the
# wall clock still bound the run.
MAX_PLAIN_REPLY_TURNS = 10


def cost_summary_from_metrics(
    metrics: dict[str, Any],
    *,
    task: str,
    model: str,
    iterations_max: int | None = None,
) -> dict[str, Any]:
    """Map SDK 1.49.2 ``Metrics.get()`` onto the June ``cost_<task>.json`` fields."""
    usage = metrics.get("accumulated_token_usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    responses = metrics.get("response_latencies")
    if not isinstance(responses, list):
        responses = metrics.get("costs") if isinstance(metrics.get("costs"), list) else []
    return {
        "task": task,
        "model": model,
        "iterations_used": len(responses),
        "iterations_max": iterations_max,
        "budget_used_usd": metrics.get("accumulated_cost"),
        "combined": {
            "accumulated_cost_usd": metrics.get("accumulated_cost"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
        },
        "per_service": {"agent": metrics},
        "raw_combined": metrics,
    }


def result_from_openhands_conversation(
    conversation: Any,
    *,
    task: str,
    model: str,
) -> dict[str, Any]:
    """Read a 1.49.2 LocalConversation after ``run()`` returns.

    ``conversation.state.execution_status`` replaces the June
    ``AgentState`` enum. ``conversation.state.stats`` replaces
    ``state.conversation_stats``. Events stay SDK objects; ``think`` is
    still recorded later as ``agent_think``.
    """
    state = conversation.state
    status = getattr(state.execution_status, "value", state.execution_status)
    status = str(status or "")
    agent_state = "FINISHED" if status == "finished" else "ERROR"
    last_error = "" if status == "finished" else status
    metrics = state.stats.get_combined_metrics().get()
    events = list(state.events)
    return {
        "events": events,
        "native_trajectory": events,
        "state": {"agent_state": agent_state, "last_error": last_error},
        "cost_summary": cost_summary_from_metrics(
            metrics,
            task=task,
            model=model,
            iterations_max=getattr(conversation, "max_iteration_per_run", None),
        ),
    }


def _exception_text(exc: BaseException) -> str:
    original = getattr(exc, "original_exception", None)
    return f"{exc} {original or ''}".lower()


def _is_run_timeout(exc: BaseException) -> bool:
    """Whether ``conversation.run`` stopped because the wall-clock cap was hit."""
    original = getattr(exc, "original_exception", None)
    if isinstance(exc, TimeoutError) or isinstance(original, TimeoutError):
        return True
    text = _exception_text(exc)
    return "timed out" in text or "timeout" in text


def _is_max_iterations(exc: BaseException) -> bool:
    """Whether the agent stopped because it used its step cap."""
    text = _exception_text(exc)
    return (
        "maxiterationsreached" in text.replace(" ", "")
        or "maximum iteration" in text
        or "max iteration" in text
    )


def system_prompt_blocks(events: list[Any]) -> list[str] | None:
    """Text blocks of the first ``SystemPromptEvent``: the prompt, then any dynamic context.

    The run folder's native trajectory keeps only the event's truncated
    ``str()``; this reads the full text the SDK sends as the system message.
    """
    for event in events:
        if type(event).__name__ != "SystemPromptEvent":
            continue
        blocks = []
        for field in ("system_prompt", "dynamic_context"):
            block = getattr(event, field, None)
            text = getattr(block, "text", None)
            if isinstance(text, str):
                blocks.append(text)
        return blocks
    return None


def pending_agent_reply(events: list[Any]) -> str | None:
    """The agent's plain reply when a run stopped to wait for the user.

    None when the last event is a tool call or its result, including the
    finish tool, or a user message.
    """
    from authorization_eva_sandbox.evaluation.harness.openhands_events import _event_kind

    for event in reversed(events):
        kind = _event_kind(event)
        if kind == "MessageEvent":
            if getattr(event, "source", None) != "agent":
                return None
            message = getattr(event, "llm_message", None)
            content = getattr(message, "content", None) or []
            text = "\n".join(getattr(b, "text", "") for b in content if getattr(b, "text", "")).strip()
            return text or None
        if kind in ("ActionEvent", "ObservationEvent"):
            return None
    return None

class OpenHandsLocalRuntime:
    """One local OpenHands 1.49.2 conversation.

    This is not the June ``create_runtime`` Docker runtime. SDK 1.49.2 uses
    ``Conversation(agent, workspace=LocalWorkspace(...))``, ``send_message``,
    and ``run``.
    """

    def __init__(
        self,
        conversation: Any,
        instruction: str,
        *,
        task: str,
        model: str,
        task_timeout_seconds: float | None = DEFAULT_TASK_TIMEOUT_SECONDS,
        user_reply_fn: Callable[[str], str | None] | None = None,
        max_user_turns: int = MAX_PLAIN_REPLY_TURNS,
        prompt_composition: Any | None = None,
        native_text: str | None = None,
        condenser_budget: dict[str, Any] | None = None,
    ):
        self.conversation = conversation
        self.prompt_composition = prompt_composition
        self.native_text = native_text
        self.condenser_budget = condenser_budget
        self.instruction = instruction
        self.task = task
        self.model = model
        self.task_timeout_seconds = task_timeout_seconds
        # June routed a plain agent reply (no tool call) to the Oracle through
        # fake_user_response_fn and kept the run going. This does the same.
        self.user_reply_fn = user_reply_fn
        self.max_user_turns = max_user_turns
        self.user_turns = 0
        self._timeout_fired = False
        self._max_iterations_fired = False

    def run(self) -> dict[str, Any]:
        self._timeout_fired = False
        self._max_iterations_fired = False
        timer = self._start_wall_clock()
        try:
            self.conversation.send_message(self.instruction)
            try:
                self._run_conversation()
            except Exception as exc:
                if _is_run_timeout(exc):
                    self._timeout_fired = True
                    self._request_pause()
                elif _is_max_iterations(exc):
                    self._max_iterations_fired = True
                else:
                    raise
            return self._result()
        except Exception:
            try:
                self.partial = self._with_prompt(result_from_openhands_conversation(
                    self.conversation,
                    task=self.task,
                    model=self.model,
                ))
            except Exception:
                self.partial = None
            raise
        finally:
            if timer is not None:
                timer.cancel()

    def _start_wall_clock(self) -> threading.Timer | None:
        timeout = self.task_timeout_seconds
        if not timeout or timeout <= 0:
            return None
        timer = threading.Timer(timeout, self._on_wall_clock)
        timer.daemon = True
        timer.start()
        return timer

    def _on_wall_clock(self) -> None:
        self._timeout_fired = True
        self._request_pause()

    def _request_pause(self) -> None:
        pause = getattr(self.conversation, "pause", None)
        if pause is None:
            return
        try:
            pause()
        except Exception:
            return

    def _run_once(self) -> None:
        run = self.conversation.run
        timeout = self.task_timeout_seconds
        if timeout and timeout > 0 and _accepts_timeout(run):
            run(timeout=timeout)
            return
        run()

    def _run_conversation(self) -> None:
        self._run_once()
        while (
            self.user_reply_fn is not None
            and not self._timeout_fired
            and self.user_turns < self.max_user_turns
        ):
            text = pending_agent_reply(list(self.conversation.state.events))
            if text is None:
                return
            reply = self.user_reply_fn(text)
            if not reply:
                return
            self.user_turns += 1
            self.conversation.send_message(reply)
            self._run_once()

    def _result(self) -> dict[str, Any]:
        produced = result_from_openhands_conversation(
            self.conversation,
            task=self.task,
            model=self.model,
        )
        if self._timeout_fired and produced["state"].get("agent_state") != "FINISHED":
            seconds = self.task_timeout_seconds or 0
            produced["state"] = {
                "agent_state": "ERROR",
                "last_error": f"wall-clock timeout after {seconds:.0f}s",
            }
        elif self._max_iterations_fired and produced["state"].get("agent_state") != "FINISHED":
            limit = getattr(self.conversation, "max_iteration_per_run", None)
            produced["state"] = {
                "agent_state": "ERROR",
                "last_error": f"Agent reached maximum iterations limit ({limit}).",
            }
        return self._with_prompt(produced)

    def _with_prompt(self, produced: dict[str, Any]) -> dict[str, Any]:
        if self.prompt_composition is None:
            return produced
        from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
            prompt_record,
        )

        metadata = dict(produced.get("metadata") or {})
        metadata["prompt"] = prompt_record(
            self.prompt_composition,
            system_blocks=system_prompt_blocks(list(produced.get("events") or [])),
            native_text=self.native_text,
        )
        if self.condenser_budget is not None:
            metadata["condenser_budget"] = dict(self.condenser_budget)
        produced["metadata"] = metadata
        return produced

    def close(self) -> None:
        self.conversation.close()


def _accepts_timeout(run: Any) -> bool:
    try:
        return "timeout" in inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
