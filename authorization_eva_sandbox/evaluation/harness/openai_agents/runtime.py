"""One OpenAI Agents run, with the same result shape as OpenHandsLocalRuntime.

``run()`` returns events, native_trajectory, state, and cost_summary.
``run_single_task`` then collects evidence, writes the trajectory, and calls
post-hoc. SDK imports stay inside the run.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from authorization_eva_sandbox.evaluation.harness.openai_agents.events import (
    ActionEvent,
    Condensation,
    MessageEvent,
    ObservationEvent,
    SystemPromptEvent,
    mcp_observation,
)
from authorization_eva_sandbox.evaluation.harness.openai_agents.prompts import LOCAL_TOOL_NAMES
from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    MAX_PLAIN_REPLY_TURNS,
    pending_agent_reply,
)

# OpenHands ends a run after four identical action/observation pairs. This
# harness does not stop there (decided 2026-09-24); it records such streaks.
_REPEAT_MARK = 4


def package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


class OpenAIAgentsRuntime:
    def __init__(
        self,
        *,
        instruction: str,
        system_prompt: str,
        model: Any,
        task: str,
        model_name: str,
        model_info: dict[str, Any] | None = None,
        model_settings: Any | None = None,
        local_tools: list[Any] | None = None,
        mcp_server: Any | None = None,
        max_steps: int = 35,
        task_timeout_seconds: float | None = DEFAULT_TASK_TIMEOUT_SECONDS,
        user_reply_fn: Callable[[str], str | None] | None = None,
        max_user_turns: int = MAX_PLAIN_REPLY_TURNS,
        condenser: Any | None = None,
        workspace: Any | None = None,
        container_difference: dict[str, Any] | None = None,
        traj_stream_path: Path | None = None,
        prompt_composition: Any | None = None,
        condenser_budget: dict[str, Any] | None = None,
    ):
        self.instruction = instruction
        self.system_prompt = system_prompt
        self.model = model
        self.task = task
        self.model_name = model_name
        self.model_info = dict(model_info or {})
        self.model_settings = model_settings
        self.local_tools = list(local_tools or [])
        self.mcp_server = mcp_server
        self.max_steps = int(max_steps)
        self.task_timeout_seconds = task_timeout_seconds
        self.user_reply_fn = user_reply_fn
        self.max_user_turns = max_user_turns
        self.condenser = condenser
        self.workspace = workspace
        self.container_difference = container_difference
        self.traj_stream_path = traj_stream_path
        self.prompt_composition = prompt_composition
        self.condenser_budget = condenser_budget
        self.user_turns = 0
        self.events: list[Any] = []
        self._item_events: list[Any] = []
        self._events_by_key: dict[str, Any] = {}
        self._seen_keys: set[str] = set()
        self._call_names: dict[str, str] = {}
        self._usage: list[dict[str, Any]] = []
        self._first_system: str | None = None
        self._first_tools: list[str] | None = None
        self._timeout = False
        self._max_turns = False
        self._output_cap = False
        self._output_cap_detail = ""
        self._stuck = ""
        self._repeats: list[dict[str, Any]] = []
        self._stop_reason = "finished"
        self.partial: dict[str, Any] | None = None
        self._seq = 0

    def run(self) -> dict[str, Any]:
        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import CostRecorder

        self.partial = None
        self._recorder = CostRecorder()
        try:
            with self._recorder.start():
                asyncio.run(self._run())
                return self._produced()
        except Exception:
            if self._stop_reason == "finished":
                self._stop_reason = "error"
            self.partial = self._produced()
            raise

    def close(self) -> None:
        workspace = self.workspace
        if workspace is not None:
            close = getattr(workspace, "close", None)
            if close is not None:
                close()

    def _make_agent(self, gated: Any, settings: Any, behavior: Any) -> Any:
        from agents import Agent

        return Agent(
            name="authorization-bench",
            instructions=self.system_prompt,
            tools=list(self.local_tools),
            mcp_servers=[self.mcp_server] if self.mcp_server is not None else [],
            mcp_config={
                "convert_schemas_to_strict": False,
                "include_server_in_tool_names": False,
            },
            model=gated,
            model_settings=settings,
            tool_use_behavior=behavior,
        )

    def _run_config(self) -> Any:
        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import run_config

        config = run_config()
        config.tool_not_found_behavior = "return_error_to_model"
        if self.condenser is not None:
            config.call_model_input_filter = self._filter_input
        return config

    async def _run(self) -> None:
        from agents import Runner
        from agents.exceptions import ModelBehaviorError
        from agents.run_error_handlers import RunErrorHandlerResult

        from authorization_eva_sandbox.evaluation.harness.openai_agents.models import (
            disable_tracing,
        )

        disable_tracing()
        settings = self.model_settings
        if settings is None:
            from agents import ModelSettings
            settings = ModelSettings()
        gated = _bound_gate(self, self.model)
        names = {getattr(tool, "name", "") for tool in self.local_tools}
        behavior: Any = "run_llm_again"
        if "finish" in names:
            behavior = {"stop_at_tool_names": ["finish"]}
        agent = self._make_agent(gated, settings, behavior)
        config = self._run_config()
        hooks = None
        if self.traj_stream_path is not None:
            from authorization_eva_sandbox.evaluation.harness.openai_agents.hooks import (
                TrajectoryStreamHooks,
            )
            hooks = TrajectoryStreamHooks(self.traj_stream_path).bind()

        self.events.append(SystemPromptEvent(text=self.system_prompt, timestamp=_now()))
        deadline = None
        if self.task_timeout_seconds and self.task_timeout_seconds > 0:
            deadline = time.monotonic() + float(self.task_timeout_seconds)
        self._deadline = deadline

        def on_max_turns(_data: Any) -> RunErrorHandlerResult:
            self._max_turns = True
            return RunErrorHandlerResult(final_output="", include_in_history=False)

        if self.mcp_server is not None:
            await self.mcp_server.connect()
        if self.condenser is not None:
            self.condenser.tools = _openai_tool_schemas(self.local_tools)
            if self.mcp_server is not None:
                self.condenser.tools.extend(await _mcp_tool_schemas(self.mcp_server))
        try:
            pending: Any = self.instruction
            # max_steps applies to each segment, as OpenHands resets its
            # iteration count on every run() call. Hitting it ends the run.
            while True:
                if self._clock_expired():
                    break
                try:
                    result = await Runner.run(
                        agent,
                        pending,
                        max_turns=self.max_steps,
                        hooks=hooks,
                        run_config=config,
                        error_handlers={"max_turns": on_max_turns},
                    )
                except ModelBehaviorError as exc:
                    if _is_output_cap(exc):
                        self._output_cap = True
                        self._output_cap_detail = str(exc)
                        break
                    raise
                self._absorb(result)
                if self._stop_now():
                    break
                reply = self._next_user_reply()
                if reply is None:
                    break
                pending = [_plain_item(item) for item in result.to_input_list()]
                pending.append({"role": "user", "content": reply})
                self._ensure_input(pending)
        finally:
            if self.mcp_server is not None:
                await self.mcp_server.cleanup()
        self._stop_reason = self._reason()

    def _filter_input(self, data: Any) -> Any:
        from agents.run_config import ModelInputData

        from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
            item_lookup_key,
        )

        model_data = data.model_data
        raw_items = model_data.input
        if isinstance(raw_items, str):
            raw_items = [{"role": "user", "content": raw_items}]
        items = [_plain_item(item) for item in raw_items]
        self._ensure_input(items)
        ids: dict[str, str] = {}
        if model_data.instructions:
            ids["system"] = "evt-system"
        for index, item in enumerate(items):
            key = item_lookup_key(item, index)
            event = self._events_by_key.get(key)
            if event is not None:
                ids[key] = str(getattr(event, "id", "") or "")
        result = self.condenser.apply(
            model_data.instructions, items, event_ids=ids,
        )
        if result.record is not None:
            self._seq += 1
            forgotten = [
                item.event_id for item in result.record.forgotten if item.event_id
            ]
            self.events.append(Condensation(
                id=f"condense-{self._seq}",
                forgotten_event_ids=forgotten,
                summary=result.record.summary,
                summary_offset=result.record.summary_offset,
                timestamp=_now(),
                trigger_tokens=result.record.trigger_tokens,
                previous_summary_forgotten=any(
                    item.kind == "summary" for item in result.record.forgotten
                ),
            ))
        if result.stuck:
            self._stuck = result.stuck_reason or "stuck: condensation made no progress"
        return ModelInputData(input=result.input_items, instructions=result.instructions)

    def _absorb(self, result: Any) -> None:
        self._ensure_input([_plain_item(item) for item in result.to_input_list()])
        self._note_repeat()

    def _ensure_input(self, items: list[Any]) -> None:
        from authorization_eva_sandbox.evaluation.harness.openai_agents.condenser import (
            _prefaces_calls,
            item_lookup_key,
        )

        for index, item in enumerate(items):
            key = item_lookup_key(item, index)
            if key in self._seen_keys:
                continue
            # A reasoning item or assistant text that only introduces a tool
            # call stays folded into that call for the condenser. The judges
            # still see the text, as its own step, ahead of the call.
            if _prefaces_calls(items, index):
                self._seen_keys.add(key)
                self._record_preface(item)
                continue
            event = self._event_for_item(item)
            self._seen_keys.add(key)
            self._events_by_key[key] = event
            self.events.append(event)
            self._item_events.append(event)

    def _record_preface(self, item: Any) -> None:
        """Judge-facing copy of text the condenser does not count as an event."""
        if not isinstance(item, dict):
            return
        kind = str(item.get("type") or item.get("role") or "")
        if kind == "reasoning":
            text = _reasoning_text(item)
            if not text:
                return
            self.events.append(ActionEvent(
                tool_name="think",
                arguments={"thought": text},
                id=_eid("think"),
                timestamp=_now(),
            ))
            return
        text = _content_text(item.get("content")).strip()
        if not text:
            return
        source = "agent" if str(item.get("role") or "") == "assistant" else "user"
        self.events.append(MessageEvent(
            source=source, text=text, id=_eid("msg"), timestamp=_now(),
        ))

    def _event_for_item(self, item: Any) -> Any | None:
        if not isinstance(item, dict):
            return MessageEvent(
                source="user", text=str(item).strip(), id=_eid("msg"), timestamp=_now(),
            )
        kind = str(item.get("type") or item.get("role") or "user")
        if kind in {"function_call", "custom_tool_call"}:
            name, arguments, call_id = _call_fields(item)
            if kind == "custom_tool_call" and isinstance(item.get("input"), str):
                arguments = {"input": item["input"]}
            self._call_names[call_id] = name
            return ActionEvent(
                tool_name=name, arguments=arguments, id=call_id, timestamp=_now(),
            )
        if kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(item.get("call_id") or _eid("call"))
            name = self._call_names.get(call_id, "")
            text = _tool_output_text(item.get("output"))
            if name not in LOCAL_TOOL_NAMES:
                text = mcp_observation(name, text)
            return ObservationEvent(
                tool_name=name, text=text, action_id=call_id, timestamp=_now(),
            )
        if kind == "reasoning":
            return ActionEvent(
                tool_name="think",
                arguments={"thought": _reasoning_text(item)},
                id=_eid("think"),
                timestamp=_now(),
            )
        text = _content_text(item.get("content")).strip()
        source = "agent" if str(item.get("role") or "") == "assistant" else "user"
        return MessageEvent(source=source, text=text, id=_eid("msg"), timestamp=_now())

    def _note_repeat(self) -> None:
        pairs: list[tuple[str, str, str]] = []
        pending: dict[str, ActionEvent] = {}
        for event in self.events:
            if isinstance(event, ActionEvent) and event.tool_name != "think":
                pending[event.id] = event
            elif isinstance(event, ObservationEvent):
                action = pending.get(event.action_id)
                if action is None:
                    continue
                pairs.append((
                    action.tool_name,
                    _jsonish(action._arguments),
                    event._text,
                ))
        streaks: list[dict[str, Any]] = []
        start = 0
        for i in range(1, len(pairs) + 1):
            if i == len(pairs) or pairs[i] != pairs[start]:
                if i - start >= _REPEAT_MARK:
                    streaks.append({"tool": pairs[start][0], "times": i - start, "first_pair": start})
                start = i
        self._repeats = streaks

    def _clock_expired(self) -> bool:
        deadline = getattr(self, "_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            self._timeout = True
            return True
        return False

    def _finished(self) -> bool:
        return bool(self.workspace is not None and getattr(self.workspace, "finished", False))

    def _stop_now(self) -> bool:
        return bool(
            self._finished() or self._timeout or self._max_turns
            or self._output_cap or self._stuck
        )

    def _next_user_reply(self) -> str | None:
        if self.user_reply_fn is None or self.user_turns >= self.max_user_turns:
            return None
        text = pending_agent_reply(self.events)
        if not text:
            return None
        reply = self.user_reply_fn(text)
        if not reply:
            return None
        self.user_turns += 1
        return reply

    def _reason(self) -> str:
        if self._output_cap:
            return "output_cap"
        if self._timeout:
            return "timeout"
        if self._max_turns:
            return "max_steps"
        if self._stuck:
            return self._stuck
        return "finished"

    def _state(self) -> dict[str, str]:
        reason = self._stop_reason
        if reason == "finished":
            return {"agent_state": "FINISHED", "last_error": ""}
        if reason == "max_steps":
            return {
                "agent_state": "ERROR",
                "last_error": f"Agent reached maximum iterations limit ({self.max_steps}).",
            }
        if reason == "timeout":
            seconds = self.task_timeout_seconds or 0
            return {
                "agent_state": "ERROR",
                "last_error": f"wall-clock timeout after {seconds:.0f}s",
            }
        if reason == "output_cap":
            return {
                "agent_state": "ERROR",
                "last_error": self._output_cap_detail
                or "output cap: finish_reason=length before any text or tool call",
            }
        return {"agent_state": "ERROR", "last_error": reason}

    def _produced(self) -> dict[str, Any]:
        recorder = getattr(self, "_recorder", None)
        calls = recorder.calls() if recorder is not None else []
        prompt = sum(int(call.usage.get("input_tokens") or 0) for call in calls)
        completion = sum(int(call.usage.get("output_tokens") or 0) for call in calls)
        reasoning = sum(int(call.usage.get("reasoning_tokens") or 0) for call in calls)
        cached = sum(int(call.usage.get("cached_tokens") or 0) for call in calls)
        if calls:
            cost = recorder.total() if recorder is not None else None
        else:
            cost = 0.0
        iterations = len(calls) or len(self._usage)
        summary = {
            "task": self.task,
            "model": self.model_name,
            "iterations_used": iterations,
            "iterations_max": self.max_steps,
            "budget_used_usd": cost,
            "combined": {
                "accumulated_cost_usd": cost,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "reasoning_tokens": reasoning,
                "cache_read_tokens": cached,
                "cache_write_tokens": sum(
                    int(call.usage.get("cache_write_tokens") or 0) for call in calls
                ),
            },
            "per_service": {"agent": {"calls": [call.usage for call in calls]}},
            "raw_combined": {"calls": [call.usage for call in calls]},
        }
        return {
            "events": list(self.events),
            "native_trajectory": [_native(event) for event in self.events],
            "state": self._state(),
            "cost_summary": summary,
            "metadata": self._metadata(),
        }

    def _metadata(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.prompt_composition is not None:
            from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
                prompt_record,
            )

            sent = None if self._first_system is None else [self._first_system]
            extra["prompt"] = prompt_record(self.prompt_composition, system_blocks=sent)
        if self.condenser_budget is not None:
            extra["condenser_budget"] = dict(self.condenser_budget)
        return {
            **extra,
            "harness": "openai_agents",
            "stop_reason": self._stop_reason,
            "sdk": {
                "openai_agents": package_version("openai-agents"),
                "litellm": package_version("litellm"),
            },
            "api_path": self.model_info.get("api_path"),
            "model_info": self.model_info,
            "termination": self._stop_reason,
            "output_cap": self._output_cap_detail or None,
            "max_steps_scope": "per_segment",
            "repeated_actions": list(self._repeats),
            "container": self.container_difference,
            "usage": list(self._usage),
            "first_request": {
                "system": self._first_system,
                "tools": self._first_tools,
            },
        }


def _bound_gate(runtime: OpenAIAgentsRuntime, inner: Any) -> Any:
    """A Model subclass. Built on first use so importing this module does not import the SDK."""
    from agents.models.interface import Model

    class _Bound(_Gate, Model):
        pass

    return _Bound(runtime, inner)


class _Gate:
    """Records the first request and ends the next turn after the wall clock.

    Non-streaming ``Runner.run`` has no after-turn cancel. The clock is checked
    at the start of the following model call, so the in-flight turn finishes.
    """

    def __init__(self, runtime: OpenAIAgentsRuntime, inner: Any):
        self.runtime = runtime
        self.inner = inner

    async def get_response(self, system_instructions: str | None, input: Any, model_settings: Any,
                           tools: list[Any], output_schema: Any, handoffs: list[Any], tracing: Any,
                           *, previous_response_id: str | None, conversation_id: str | None,
                           prompt: Any) -> Any:
        names = [name for name in (getattr(tool, "name", None) for tool in tools) if name]
        if self.runtime._first_system is None and self.runtime._first_tools is None:
            self.runtime._first_system = system_instructions
            self.runtime._first_tools = names
        if self.runtime._clock_expired() or self.runtime._stuck:
            from agents.items import ModelResponse
            from agents.usage import Usage
            from openai.types.responses import ResponseOutputMessage, ResponseOutputText

            note = self.runtime._stuck or "wall-clock timeout"
            message = ResponseOutputMessage(
                id="m-stop",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text=note, annotations=[])],
            )
            return ModelResponse(output=[message], usage=Usage(), response_id="stop")
        response = await self.inner.get_response(
            system_instructions, input, model_settings, tools, output_schema, handoffs, tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        usage = getattr(response, "usage", None)
        details = getattr(usage, "output_tokens_details", None) if usage is not None else None
        self.runtime._usage.append({
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "reasoning_tokens": getattr(details, "reasoning_tokens", None) if details else None,
            "response_id": getattr(response, "response_id", None),
        })
        return response

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("the openai_agents harness does not stream model responses")

    async def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close is not None:
            await close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _eid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _openai_tool_schemas(tools: list[Any]) -> list[dict[str, Any]]:
    schemas = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not name:
            continue
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": getattr(tool, "description", "") or "",
                "parameters": getattr(tool, "params_json_schema", None)
                or {"type": "object", "properties": {}},
            },
        })
    return schemas


async def _mcp_tool_schemas(server: Any) -> list[dict[str, Any]]:
    try:
        listed = await server.list_tools()
    except Exception:
        return []
    schemas = []
    for tool in listed or []:
        name = getattr(tool, "name", None)
        if not name:
            continue
        parameters = (
            getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
            or {"type": "object", "properties": {}}
        )
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": getattr(tool, "description", "") or "",
                "parameters": parameters,
            },
        })
    return schemas


def _is_output_cap(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "length" in text or "output cap" in text


def _tool_output_text(value: Any) -> str:
    """The text a tool result shows the model. MCP results arrive as content blocks."""
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        text = "\n".join(part for part in parts if part)
        return text or _jsonish(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return _tool_output_text(parsed)
        return value
    return _jsonish(value)


def _jsonish(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except TypeError:
        return str(value)


def _plain_item(item: Any) -> Any:
    if isinstance(item, dict) or isinstance(item, str):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_unset=True)
        except TypeError:
            data = dump()
        if isinstance(data, dict):
            return data
    return item


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _call_fields(item: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    import json
    name = str(item.get("name") or "")
    arguments = item.get("arguments") or {}
    call_id = str(item.get("call_id") or item.get("id") or _eid("call"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": arguments}
    if not isinstance(arguments, dict):
        arguments = {"_raw": arguments}
    return name, arguments, call_id


def _reasoning_text(raw: Any) -> str:
    if isinstance(raw, dict):
        blocks = raw.get("summary") or raw.get("content") or []
    else:
        blocks = getattr(raw, "summary", None) or getattr(raw, "content", None) or []
    parts = []
    for block in blocks:
        if isinstance(block, dict):
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(part for part in parts if part).strip()


def _native(event: Any) -> Any:
    as_dict = getattr(event, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return {"repr": str(event)}
