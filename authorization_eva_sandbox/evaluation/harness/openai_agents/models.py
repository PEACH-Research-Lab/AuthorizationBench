"""Model construction for the OpenAI Agents SDK harness.

Direct OpenAI profiles use the Responses API. OpenRouter profiles use one of
two chat backends, selected by ``chat_backend``:

* ``litellm`` — ``LitellmModel`` with an ``openrouter/`` model id
* ``native`` — ``OpenAIChatCompletionsModel`` against the profile base URL

The module imports cleanly where ``agents`` is not installed. SDK symbols are
loaded on first use, and fields that exist only on newer releases
(``ModelSettings.timeout``) are set only when that release has them.
"""
from __future__ import annotations

import copy
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

# One model call. OpenHands waits 300s and retries five times; a stalled
# provider then holds the run for many minutes, so this harness stops sooner.
CALL_TIMEOUT_SECONDS = 150.0
MAX_MODEL_RETRIES = 2

ChatBackend = str  # "litellm" | "native"

_active_recorder: ContextVar["CostRecorder | None"] = ContextVar(
    "openai_agents_cost_recorder", default=None
)
_litellm_callback_installed = False


@dataclass(frozen=True)
class ModelInfo:
    """What a run used, for the run metadata."""

    api_path: str
    model_id: str
    effort: str | None
    max_output_tokens: int | None
    provider_lock: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_path": self.api_path,
            "model_id": self.model_id,
            "effort": self.effort,
            "max_output_tokens": self.max_output_tokens,
            "provider_lock": self.provider_lock,
        }


@dataclass(frozen=True)
class CallCost:
    cost_usd: float | None
    usage: dict[str, Any]


class CostRecorder:
    """Per-run cost. Concurrent runs in one process each ``start()`` their own.

    LiteLLM calls are recorded by a success callback (``response_cost``).
    Responses calls are priced with ``litellm.cost_per_token``, the same
    ``model_cost`` table OpenHands uses. Native Chat Completions calls take
    ``cost`` from ``raw_usage`` when the SDK preserved it. A missing price
    is ``None`` and does not fail the run.
    """

    def __init__(self) -> None:
        self._calls: list[CallCost] = []

    @contextmanager
    def start(self) -> Iterator["CostRecorder"]:
        self._calls.clear()
        _ensure_litellm_callback()
        token = _active_recorder.set(self)
        try:
            yield self
        finally:
            _active_recorder.reset(token)

    def calls(self) -> list[CallCost]:
        return list(self._calls)

    def total(self) -> float | None:
        if not self._calls or any(call.cost_usd is None for call in self._calls):
            return None
        return float(sum(call.cost_usd or 0.0 for call in self._calls))

    def _append(self, cost_usd: float | None, usage: dict[str, Any]) -> None:
        self._calls.append(CallCost(cost_usd=cost_usd, usage=dict(usage)))


def disable_tracing() -> None:
    """Stop the SDK from exporting traces, including tool inputs and outputs."""
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    from agents import set_tracing_disabled

    set_tracing_disabled(True)


def run_config() -> Any:
    from agents import RunConfig

    return RunConfig(tracing_disabled=True, trace_include_sensitive_data=False)


def model_for_profile(
    profile: dict[str, Any], *, chat_backend: str
) -> tuple[Any, Any, ModelInfo]:
    """``(model, ModelSettings, info)`` for one evaluation profile.

    ``chat_backend`` chooses the OpenRouter path. Direct OpenAI profiles
    always use Responses, on either setting.
    """
    if chat_backend not in ("litellm", "native"):
        raise ValueError(f"chat_backend must be 'litellm' or 'native', got {chat_backend!r}")
    if _is_openrouter(profile):
        if chat_backend == "litellm":
            return _litellm_model(profile)
        return _native_chat_model(profile)
    return _responses_model(profile)


def _profile_extra_body(profile: dict[str, Any]) -> dict[str, Any]:
    completion_kwargs = profile.get("completion_kwargs") or {}
    extra_body = completion_kwargs.get("extra_body") if isinstance(completion_kwargs, dict) else None
    return copy.deepcopy(extra_body) if isinstance(extra_body, dict) else {}


def _effort_from_profile(profile: dict[str, Any]) -> str | None:
    """The one reasoning effort a profile asks for.

    A profile may state it as ``reasoning_effort`` or, in older profiles, as
    ``extra_body.reasoning.effort``. OpenRouter rejects a request that carries
    both, so two different values are a config error and the nested copy is
    removed before the request is built.
    """
    top = profile.get("reasoning_effort")
    reasoning = _profile_extra_body(profile).get("reasoning")
    nested = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if top and nested and str(top) != str(nested):
        raise ValueError(
            f"profile sets reasoning_effort={top!r} and "
            f"extra_body.reasoning.effort={nested!r}; keep one"
        )
    effort = top or nested
    return str(effort) if effort else None


def _output_cap(profile: dict[str, Any]) -> int | None:
    raw = profile.get("max_output_tokens")
    return int(raw) if raw else None


def _provider_lock(profile: dict[str, Any]) -> dict[str, Any] | None:
    provider = _profile_extra_body(profile).get("provider")
    return copy.deepcopy(provider) if isinstance(provider, dict) else None


def _chat_extra_body(profile: dict[str, Any]) -> dict[str, Any]:
    extra = _profile_extra_body(profile)
    reasoning = extra.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        rest = {key: value for key, value in reasoning.items() if key != "effort"}
        if rest:
            extra["reasoning"] = rest
        else:
            extra.pop("reasoning")
    # OpenRouter returns a dollar cost only when usage accounting is requested.
    # LiteLLM 1.102 injects the same object; the native client does not.
    extra.setdefault("usage", {"include": True})
    return extra


def _is_openrouter(profile: dict[str, Any]) -> bool:
    return str(profile.get("model") or "").startswith("openrouter/")


def _settings_have_timeout() -> bool:
    from agents import ModelSettings

    return "timeout" in getattr(ModelSettings, "__dataclass_fields__", {})


def _retry_settings() -> Any:
    from agents import ModelRetrySettings
    from agents.retry import ModelRetryBackoffSettings, retry_policies

    return ModelRetrySettings(
        max_retries=MAX_MODEL_RETRIES,
        backoff=ModelRetryBackoffSettings(
            initial_delay=1.0, multiplier=2.0, max_delay=8.0, jitter=False
        ),
        policy=retry_policies.any(
            retry_policies.network_error(),
            retry_policies.http_status([429, *range(500, 600)]),
        ),
    )


def _model_settings(
    *,
    effort: str | None,
    cap: int | None,
    extra_body: dict[str, Any] | None,
    extra_args: dict[str, Any] | None,
    include_usage: bool,
) -> Any:
    from agents import ModelSettings
    from openai.types.shared import Reasoning

    kwargs: dict[str, Any] = {
        "retry": _retry_settings(),
        "preserve_raw_usage": True,
    }
    if effort:
        kwargs["reasoning"] = Reasoning(effort=effort)  # type: ignore[arg-type]
    if cap is not None:
        kwargs["max_tokens"] = cap
    if extra_body:
        kwargs["extra_body"] = extra_body
    if extra_args:
        kwargs["extra_args"] = extra_args
    if include_usage:
        kwargs["include_usage"] = True
    if _settings_have_timeout():
        kwargs["timeout"] = CALL_TIMEOUT_SECONDS
    return ModelSettings(**kwargs)


def _responses_model(profile: dict[str, Any]) -> tuple[Any, Any, ModelInfo]:
    from openai import AsyncOpenAI

    model_id = str(profile["model"])
    effort = _effort_from_profile(profile)
    cap = _output_cap(profile)
    # 0.20 has no ModelSettings.timeout. responses.create() takes timeout as a
    # transport option, so it does not become a second body field.
    extra_args = None if _settings_have_timeout() else {"timeout": CALL_TIMEOUT_SECONDS}
    client = AsyncOpenAI(
        api_key=str(profile.get("api_key") or ""),
        base_url=profile.get("base_url") or None,
        timeout=CALL_TIMEOUT_SECONDS,
    )
    model = _responses_recorder()(model=model_id, openai_client=client)
    settings = _model_settings(
        effort=effort, cap=cap, extra_body=None, extra_args=extra_args, include_usage=False
    )
    info = ModelInfo("responses", model_id, effort, cap, None)
    return model, settings, info


def _litellm_model(profile: dict[str, Any]) -> tuple[Any, Any, ModelInfo]:
    _ensure_litellm_callback()
    from agents.extensions.models.litellm_model import LitellmModel

    model_id = str(profile["model"])
    effort = _effort_from_profile(profile)
    cap = _output_cap(profile)
    # Runner retries are the only retry layer. num_retries stays 0 on the
    # first attempt as well, not only on the SDK's replay attempts.
    # ModelSettings.timeout is absent on 0.20 and, where it exists, is an
    # asyncio deadline rather than the LiteLLM HTTP timeout.
    extra_args: dict[str, Any] = {
        "timeout": CALL_TIMEOUT_SECONDS,
        "num_retries": 0,
        "max_retries": 0,
    }
    model = _litellm_recorder()(
        model=model_id,
        api_key=str(profile.get("api_key") or ""),
        base_url=profile.get("base_url") or None,
    )
    settings = _model_settings(
        effort=effort,
        cap=cap,
        extra_body=_chat_extra_body(profile),
        extra_args=extra_args,
        include_usage=True,
    )
    info = ModelInfo("litellm_chat", model_id, effort, cap, _provider_lock(profile))
    return model, settings, info


def _native_chat_model(profile: dict[str, Any]) -> tuple[Any, Any, ModelInfo]:
    from openai import AsyncOpenAI

    full_id = str(profile["model"])
    model_id = full_id.removeprefix("openrouter/")
    effort = _effort_from_profile(profile)
    cap = _output_cap(profile)
    client = AsyncOpenAI(
        api_key=str(profile.get("api_key") or ""),
        base_url=profile.get("base_url") or None,
        timeout=CALL_TIMEOUT_SECONDS,
    )
    model = _native_recorder()(model=model_id, openai_client=client)
    settings = _model_settings(
        effort=effort,
        cap=cap,
        extra_body=_chat_extra_body(profile),
        extra_args=None,
        include_usage=True,
    )
    info = ModelInfo("native_chat", model_id, effort, cap, _provider_lock(profile))
    return model, settings, info


def _responses_recorder():
    from agents import OpenAIResponsesModel

    class _RecordingResponses(OpenAIResponsesModel):
        async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
            response = await super()._fetch_response(*args, **kwargs)
            if not kwargs.get("stream"):
                _raise_if_responses_output_capped(response)
            return response

        async def get_response(self, *args: Any, **kwargs: Any) -> Any:
            response = await super().get_response(*args, **kwargs)
            _record_sdk_response(response, kind="responses", model_id=str(self.model))
            return response

    return _RecordingResponses


def _native_recorder():
    from agents import OpenAIChatCompletionsModel

    class _RecordingChat(OpenAIChatCompletionsModel):
        async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
            response = await super()._fetch_response(*args, **kwargs)
            if not kwargs.get("stream"):
                _raise_if_length_capped(response)
            return response

        async def get_response(self, *args: Any, **kwargs: Any) -> Any:
            response = await super().get_response(*args, **kwargs)
            _record_sdk_response(response, kind="native", model_id=str(self.model))
            return response

    return _RecordingChat


def _litellm_recorder():
    from agents.extensions.models.litellm_model import LitellmModel

    class _RecordingLitellm(LitellmModel):
        async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
            import litellm

            current = litellm.acompletion
            cache = _explicit_prompt_cache(str(self.model))

            async def _call(*call_args: Any, **call_kwargs: Any) -> Any:
                if cache:
                    messages = call_kwargs.get("messages")
                    if isinstance(messages, list):
                        apply_openhands_prompt_cache(messages)
                return await current(*call_args, **call_kwargs)

            litellm.acompletion = _call
            try:
                response = await super()._fetch_response(*args, **kwargs)
            finally:
                litellm.acompletion = current
            if not kwargs.get("stream"):
                _raise_if_length_capped(response)
            return response

    return _RecordingLitellm


def _explicit_prompt_cache(model_id: str) -> bool:
    """OpenHands caches only models in ``PROMPT_CACHE_MODELS``.

    That list is Claude. Gemini is left out on purpose: an explicit
    ``cache_control`` marker freezes its cache on the static prefix and turns
    off implicit caching of the growing body. GPT caches on the Responses
    path without markers. GLM (locked to z-ai) is not in the list and is
    left alone.
    """
    return "claude" in model_id.lower()


_CACHE_CONTROL = {"type": "ephemeral"}


def apply_openhands_prompt_cache(messages: list[dict[str, Any]]) -> None:
    """The breakpoints ``LLM._apply_prompt_caching`` sets, when caching is on.

    The system message's first content block is marked. A second system block
    is left unmarked. The last user or tool message is marked: on a tool
    message the marker sits on the message, otherwise on its last content block.
    """
    if messages and messages[0].get("role") == "system":
        _mark_system_block(messages[0])
    for message in reversed(messages):
        if message.get("role") in ("user", "tool"):
            _mark_latest_turn(message)
            break


def _mark_system_block(message: dict[str, Any]) -> None:
    content = _as_blocks(message)
    if not content:
        return
    _set_cache(content[0], True)
    if len(content) >= 2:
        _set_cache(content[1], False)


def _mark_latest_turn(message: dict[str, Any]) -> None:
    if message.get("role") == "tool":
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
        message["cache_control"] = dict(_CACHE_CONTROL)
        return
    content = _as_blocks(message)
    if content:
        _set_cache(content[-1], True)


def _as_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
        message["content"] = blocks
        return blocks
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _set_cache(block: dict[str, Any], enabled: bool) -> None:
    if enabled:
        block["cache_control"] = dict(_CACHE_CONTROL)
    else:
        block.pop("cache_control", None)


def _raise_if_length_capped(response: Any) -> None:
    """0.20.0 does not raise when a chat response is length-capped and empty.

    Later docs do. An empty ``finish_reason=length`` becomes a
    ``ModelBehaviorError`` so the run ends as an output-cap termination.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return
    choice = choices[0]
    if getattr(choice, "finish_reason", None) != "length":
        return
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    tool_calls = getattr(message, "tool_calls", None) if message is not None else None
    if content or tool_calls:
        return
    from agents.exceptions import ModelBehaviorError

    raise ModelBehaviorError(
        "output cap: finish_reason=length before any text or tool call"
    )


def _raise_if_responses_output_capped(response: Any) -> None:
    """0.20.0 returns a Responses payload with status ``incomplete`` instead of raising.

    ``incomplete_details.reason == max_output_tokens`` ends the run the same
    way an empty chat ``finish_reason=length`` does.
    """
    status = getattr(response, "status", None)
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None)
    if reason is None and isinstance(details, dict):
        reason = details.get("reason")
    if status != "incomplete" or reason != "max_output_tokens":
        return
    from agents.exceptions import ModelBehaviorError

    raise ModelBehaviorError(
        "output cap: responses status=incomplete reason=max_output_tokens"
    )


def _record_sdk_response(response: Any, *, kind: str, model_id: str) -> None:
    recorder = _active_recorder.get()
    if recorder is None:
        return
    usage = _usage_from_sdk(getattr(response, "usage", None))
    if kind == "responses":
        cost = _responses_cost(model_id, usage)
    else:
        raw = getattr(response, "raw_usage", None) or {}
        cost = _as_float(raw.get("cost")) if isinstance(raw, dict) else None
    recorder._append(cost, usage)


def _responses_cost(model_id: str, usage: dict[str, Any]) -> float | None:
    """Price one Responses call from litellm's model_cost table."""
    try:
        import litellm

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model_id,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            cache_read_input_tokens=int(usage.get("cached_tokens") or 0),
        )
    except Exception:
        return None
    return float(prompt_cost) + float(completion_cost)


def _usage_from_sdk(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    out_details = getattr(usage, "output_tokens_details", None)
    in_details = getattr(usage, "input_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "reasoning_tokens": getattr(out_details, "reasoning_tokens", None) if out_details else None,
        "cached_tokens": getattr(in_details, "cached_tokens", None) if in_details else None,
    }


def _usage_from_litellm(response_obj: Any) -> dict[str, Any]:
    usage = getattr(response_obj, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        details = usage.get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached = prompt_details.get("cached_tokens")
        if cached is None:
            cached = usage.get("cache_read_input_tokens")
        cache_write = prompt_details.get("cache_write_tokens")
        if cache_write is None:
            cache_write = usage.get("cache_creation_input_tokens")
    else:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        details = getattr(usage, "completion_tokens_details", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None) if details else None
        cached = getattr(prompt_details, "cached_tokens", None) if prompt_details else None
        cache_write = getattr(prompt_details, "cache_write_tokens", None) if prompt_details else None
        if cached is None:
            cached = getattr(usage, "cache_read_input_tokens", None)
        if cache_write is None:
            cache_write = getattr(usage, "cache_creation_input_tokens", None)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "reasoning_tokens": reasoning,
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_litellm(kwargs: dict[str, Any], response_obj: Any) -> None:
    recorder = _active_recorder.get()
    if recorder is None:
        return
    recorder._append(_as_float(kwargs.get("response_cost")), _usage_from_litellm(response_obj))


def _ensure_litellm_callback() -> None:
    global _litellm_callback_installed
    if _litellm_callback_installed:
        return
    try:
        import litellm
        from litellm.integrations.custom_logger import CustomLogger
    except ImportError:
        return

    class _LitellmCostLogger(CustomLogger):
        def log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
            _record_litellm(kwargs, response_obj)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
            _record_litellm(kwargs, response_obj)

    litellm.callbacks.append(_LitellmCostLogger())
    _litellm_callback_installed = True
