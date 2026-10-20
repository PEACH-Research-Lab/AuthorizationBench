"""OpenHands 1.49.2 LLMSummarizingCondenser, on the Agents SDK input filter.

The template is the verbatim ``summarizing_prompt.j2`` shipped with
openhands-sdk 1.49.2 (``openhands/sdk/context/condenser/prompts``). A
condensation summary is a user message, which is ``CondensationSummaryEvent.to_llm_message``.

The view matches the 1.49.2 event log: one event for the system prompt, one
per user or agent message, one per tool call, and one per observation.
Reasoning items are not events; they travel with the following tool call.
Parallel tool calls share a batch id, so a cut cannot fall inside the batch.
``keep_first`` indexes that view. With ``keep_first=0`` the first event is
the system prompt, and the first condensation can forget it
(``summary_offset`` 0).

Token counts follow ``get_total_token_count``: messages are the chat dicts
``LLM.format_messages_for_llm`` would build, and tool schemas are included
only while a system-prompt event is still in the view.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROMPT_PATH = Path(__file__).with_name("prompts") / "summarizing_prompt.j2"
# openhands-sdk 1.49.2 LLMSummarizingCondenser defaults that this mapping uses.
MINIMUM_PROGRESS = 0.1
HARD_RESET_RETRIES = 5
HARD_RESET_SCALING = 0.8
# str(LLMConvertibleEvent) keeps this many characters of each event.
EVENT_PREVIEW_CHARS = 500
# openhands-sdk 1.49.2 PROMPT_CACHE_MODELS. Substring match, as model_matches.
PROMPT_CACHE_MODELS = (
    "claude-3-7-sonnet",
    "claude-sonnet-3-7-latest",
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "claude-3-haiku-20240307",
    "claude-3-opus-20240229",
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
)
# Models whose chat content must be a string. 1.49.2 FORCE_STRING_SERIALIZER_MODELS.
STRING_CONTENT_MODELS = (
    "deepseek",
    "glm",
    "groq/kimi-k2-instruct",
    "openrouter/minimax",
)

Summarizer = Callable[[str], str]
TokenCounter = Callable[..., int]


@dataclass
class ViewItem:
    """One OpenHands view event the condenser can forget or keep."""

    key: str
    kind: str
    text: str
    call_id: str | None = None
    raw: Any = None
    event_id: str | None = None
    batch_id: str | None = None
    tool_name: str | None = None
    thought: str = ""
    # SDK items that belong to this event but are not themselves events
    # (a reasoning item, or assistant text folded into a tool call).
    prefix_raw: list[Any] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        if self.kind == "function_call":
            return _tool_call_message([self])
        if self.kind == "function_call_output":
            return _tool_result_message(self)
        role = {
            "system": "system",
            "assistant": "assistant",
            "summary": "user",
        }.get(self.kind, "user")
        return _text_message(role, self.text or "")


@dataclass
class CondensationRecord:
    forgotten: list[ViewItem]
    summary: str
    summary_offset: int
    forgotten_keys: set[str] = field(default_factory=set)
    # Token count of the view that exceeded max_tokens. Recording only.
    trigger_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.forgotten_keys:
            self.forgotten_keys = {item.key for item in self.forgotten}


@dataclass
class CondenserResult:
    instructions: str | None
    input_items: list[Any]
    record: CondensationRecord | None
    stuck: bool
    stuck_reason: str


class SummarizingCondenser:
    """One run's condenser. Saved summaries are reapplied on later calls."""

    def __init__(
        self,
        *,
        max_tokens: int,
        keep_first: int,
        model: str,
        summarize: Summarizer,
        count_tokens: TokenCounter | None = None,
        tools: list[dict[str, Any]] | None = None,
    ):
        self.max_tokens = int(max_tokens)
        self.keep_first = int(keep_first)
        self.model = model
        self.summarize = summarize
        self.tools = tools or []
        if count_tokens is None:
            self._count = lambda messages, schemas=None: _litellm_count(
                self.model, messages, schemas or [],
            )
            self._count_with_tools = True
        else:
            self._count = count_tokens
            self._count_with_tools = _accepts_tools(count_tokens)
        self.saved: list[CondensationRecord] = []
        self.unhelpful = 0

    def apply(
        self,
        instructions: str | None,
        input_items: list[Any],
        *,
        event_ids: dict[str, str] | None = None,
    ) -> CondenserResult:
        """Return the input to send, condensing once when over budget."""
        ids = event_ids or {}
        view = _parse_view(instructions, input_items, ids)
        for saved in self.saved:
            view = _apply_record(view, saved)
        record: CondensationRecord | None = None
        stuck = False
        reason = ""
        before = self._tokens(view)
        if before > self.max_tokens:
            record = self._condense(view)
            if record is None:
                stuck = True
                reason = "stuck: condensation made no progress"
            else:
                record.trigger_tokens = before
                condensed = _apply_record(view, record)
                after = self._tokens(condensed)
                if after >= before:
                    self.unhelpful += 1
                else:
                    self.unhelpful = 0
                if self.unhelpful >= 2:
                    stuck = True
                    reason = "stuck: condensation did not reduce the context"
                self.saved.append(record)
                view = condensed
        return CondenserResult(
            instructions=_instructions_of(view),
            input_items=_input_of(view),
            record=record,
            stuck=stuck,
            stuck_reason=reason,
        )

    def _tokens(self, view: list[ViewItem]) -> int:
        messages = messages_for_token_count(view, model=self.model)
        # The first system item is the SystemPromptEvent. Once it is forgotten,
        # OpenHands stops passing its tool schemas into the token counter.
        schemas = self.tools if any(item.kind == "system" for item in view) else None
        if self._count_with_tools:
            return int(self._count(messages, schemas))
        return int(self._count(messages))

    def _condense(self, view: list[ViewItem]) -> CondensationRecord | None:
        try:
            forgotten, offset = _forgotten_range(
                view,
                keep_first=self.keep_first,
                max_tokens=self.max_tokens,
                count=self._tokens,
            )
        except ValueError:
            forgotten, offset = [], 0
        if not forgotten or len(forgotten) < len(view) * MINIMUM_PROGRESS:
            return self._hard_reset(view)
        summary = self._summary_text(forgotten, None)
        if not summary:
            return self._hard_reset(view)
        return CondensationRecord(forgotten, summary, offset)

    def _hard_reset(self, view: list[ViewItem]) -> CondensationRecord | None:
        """Summarize the whole view, shortening event text when the call fails."""
        if not view:
            return None
        limit: int | None = None
        for _ in range(HARD_RESET_RETRIES):
            summary = self._summary_text(view, limit)
            if summary:
                return CondensationRecord(list(view), summary, 0)
            longest = max(len(_event_string(item)) for item in view)
            limit = int((limit or longest) * HARD_RESET_SCALING)
            if limit < 32:
                break
        return None

    def _summary_text(self, forgotten: list[ViewItem], limit: int | None) -> str:
        events = [_truncate(_event_string(item), limit) for item in forgotten]
        prompt = render_summarizing_prompt(events)
        try:
            text = self.summarize(prompt)
        except Exception:
            return ""
        return (text or "").strip()


def render_summarizing_prompt(events: list[str]) -> str:
    """Render the copied template. Jinja does not escape these strings."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    env = Environment(
        loader=FileSystemLoader(PROMPT_PATH.parent),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=False,
    )
    rendered = env.get_template(PROMPT_PATH.name).render(events=events)
    return rendered.strip()


def _event_string(item: ViewItem) -> str:
    text = item.text or ""
    if len(text) > EVENT_PREVIEW_CHARS:
        text = text[: EVENT_PREVIEW_CHARS - 3] + "..."
    role = "user" if item.kind in {"summary", "user", "system"} else item.kind
    return f"{item.kind}\n  {role}: {text}"


def _truncate(text: str, limit: int | None) -> str:
    if not limit or len(text) <= limit:
        return text
    notice = (
        "<response clipped><NOTE>Due to the max output limit, only part of the full "
        "response has been shown to you.</NOTE>"
    )
    if len(notice) >= limit:
        return notice[:limit]
    remaining = limit - len(notice)
    head = remaining // 2 + remaining % 2
    tail = remaining - head
    return text[:head] + notice + (text[-tail:] if tail else "")


def _accepts_tools(counter: TokenCounter) -> bool:
    try:
        signature = inspect.signature(counter)
    except (TypeError, ValueError):
        return False
    if any(
        param.kind == inspect.Parameter.VAR_POSITIONAL
        for param in signature.parameters.values()
    ):
        return True
    positional = [
        param for param in signature.parameters.values()
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 2


def _litellm_count(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    import litellm

    return int(litellm.token_counter(model=model, messages=messages, tools=tools or None))


def _model_has(model: str, patterns: tuple[str, ...]) -> bool:
    raw = (model or "").lower()
    return any(pattern.lower() in raw for pattern in patterns)


def _string_content(model: str) -> bool:
    return _model_has(model, STRING_CONTENT_MODELS)


# OpenHands MCP observations are two text blocks: the execution line, then the body.
_MCP_OBSERVATION = re.compile(r"^\[Tool '[^']+' executed\.\]\n", re.DOTALL)


def _content_value(text: str, *, string_content: bool) -> str | list[dict[str, str]]:
    if string_content:
        return text
    match = _MCP_OBSERVATION.match(text or "")
    if match:
        return [
            {"type": "text", "text": text[: match.end() - 1]},
            {"type": "text", "text": text[match.end():]},
        ]
    return [{"type": "text", "text": text}]


def _text_message(role: str, text: str, *, string_content: bool = False) -> dict[str, Any]:
    if role == "assistant" and not text.strip():
        return {"role": role, "content": ""}
    return {"role": role, "content": _content_value(text, string_content=string_content)}


def _tool_call_dict(item: ViewItem) -> dict[str, Any]:
    return {
        "id": item.call_id or "",
        "type": "function",
        "function": {
            "name": item.tool_name or "",
            "arguments": item.text or "",
        },
    }


def _tool_call_message(batch: list[ViewItem], *, string_content: bool = False) -> dict[str, Any]:
    """One assistant message for a parallel tool-call batch, as ``events_to_messages``."""
    message: dict[str, Any] = {
        "role": "assistant",
        "tool_calls": [_tool_call_dict(item) for item in batch],
    }
    thought = next((item.thought for item in batch if item.thought.strip()), "")
    if thought.strip():
        message["content"] = _content_value(thought, string_content=string_content)
    return message


def _tool_result_message(item: ViewItem, *, string_content: bool = False) -> dict[str, Any]:
    return {
        "role": "tool",
        "content": _content_value(item.text or "", string_content=string_content),
        "tool_call_id": item.call_id or "",
        "name": item.tool_name or "",
    }


def messages_for_token_count(view: list[ViewItem], *, model: str) -> list[dict[str, Any]]:
    """Chat messages ``LLM.format_messages_for_llm`` would count for this view."""
    string_content = _string_content(model)
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(view):
        item = view[index]
        if item.kind == "function_call":
            batch = [item]
            index += 1
            while (
                index < len(view)
                and view[index].kind == "function_call"
                and item.batch_id
                and view[index].batch_id == item.batch_id
            ):
                batch.append(view[index])
                index += 1
            messages.append(_tool_call_message(batch, string_content=string_content))
            continue
        if item.kind == "function_call_output":
            messages.append(_tool_result_message(item, string_content=string_content))
        else:
            role = "assistant" if item.kind == "assistant" else (
                "system" if item.kind == "system" else "user"
            )
            messages.append(_text_message(role, item.text or "", string_content=string_content))
        index += 1
    if _model_has(model, PROMPT_CACHE_MODELS):
        _apply_prompt_cache(messages)
    return messages


def _apply_prompt_cache(messages: list[dict[str, Any]]) -> None:
    """1.49.2 ``LLM._apply_prompt_caching`` for a single system block and the tail."""
    if messages and messages[0].get("role") == "system":
        _mark_cache(messages[0], first_block_only=True)
    for message in reversed(messages):
        if message.get("role") in {"user", "tool"}:
            _mark_cache(message, first_block_only=False)
            break


def _mark_cache(message: dict[str, Any], *, first_block_only: bool) -> None:
    content = message.get("content")
    if isinstance(content, list) and content:
        block = content[0] if first_block_only else content[-1]
        if isinstance(block, dict):
            if message.get("role") == "tool":
                message["cache_control"] = {"type": "ephemeral"}
            else:
                block["cache_control"] = {"type": "ephemeral"}
    elif isinstance(content, str) and content and message.get("role") != "tool":
        message["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]


def _parse_view(
    instructions: str | None,
    input_items: list[Any],
    event_ids: dict[str, str],
) -> list[ViewItem]:
    """One view event per system prompt, message, tool call, and observation.

    A reasoning item, and assistant text that only introduces a tool call,
    are not events. They are kept on the first call of that batch so the
    model input can still echo them, and ``keep_first`` does not see them.
    """
    view: list[ViewItem] = []
    if instructions:
        view.append(ViewItem(
            key="system",
            kind="system",
            text=instructions,
            event_id=event_ids.get("system"),
        ))
    names: dict[str, str] = {}
    index = 0
    batch_number = 0
    items = list(input_items)
    while index < len(items):
        kind = _item_kind(items[index])
        if kind == "function_call_output":
            view.append(_output_item(items[index], index, event_ids, names))
            index += 1
            continue
        if kind == "function_call" or _prefaces_calls(items, index):
            preface: list[Any] = []
            preface_at: list[int] = []
            thought_parts: list[str] = []
            while index < len(items) and _is_call_preface(items[index]):
                preface.append(items[index])
                preface_at.append(index)
                if _item_kind(items[index]) != "reasoning":
                    text = _content_text(items[index].get("content") if isinstance(items[index], dict) else None)
                    if text.strip():
                        thought_parts.append(text)
                index += 1
            calls: list[tuple[int, Any]] = []
            while index < len(items) and _item_kind(items[index]) == "function_call":
                calls.append((index, items[index]))
                index += 1
            if not calls:
                for at, raw in zip(preface_at, preface):
                    if _item_kind(raw) == "reasoning" and not _reasoning_text(raw).strip():
                        continue
                    view.append(_item_to_view(raw, at, event_ids))
                continue
            batch_number += 1
            batch_id = f"batch-{batch_number}"
            thought = "\n".join(thought_parts)
            for offset, (at, raw) in enumerate(calls):
                item = _call_item(raw, at, event_ids, batch_id)
                names[item.call_id or ""] = item.tool_name or ""
                if offset == 0:
                    item.thought = thought
                    item.prefix_raw = list(preface)
                view.append(item)
            continue
        view.append(_item_to_view(items[index], index, event_ids))
        index += 1
    return view


def _item_kind(item: Any) -> str:
    if not isinstance(item, dict):
        return "user"
    return str(item.get("type") or item.get("role") or "user")


def _is_call_preface(item: Any) -> bool:
    kind = _item_kind(item)
    if kind == "reasoning":
        return True
    if not isinstance(item, dict):
        return False
    role = str(item.get("role") or "")
    return role == "assistant" and kind not in {"function_call", "function_call_output"}


def _prefaces_calls(items: list[Any], index: int) -> bool:
    """True when this preface is followed by a tool call before another turn."""
    if not _is_call_preface(items[index]):
        return False
    cursor = index
    while cursor < len(items) and _is_call_preface(items[cursor]):
        cursor += 1
    return cursor < len(items) and _item_kind(items[cursor]) == "function_call"


def _call_item(item: dict[str, Any], index: int, event_ids: dict[str, str], batch_id: str) -> ViewItem:
    key = item_lookup_key(item, index)
    call_id = str(item.get("call_id") or item.get("id") or index)
    name = str(item.get("name") or "")
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        import json
        arguments = json.dumps(arguments if arguments is not None else {})
    return ViewItem(
        key, "function_call", arguments, call_id, item, event_ids.get(key),
        batch_id=batch_id, tool_name=name,
    )


def _output_item(
    item: Any, index: int, event_ids: dict[str, str], names: dict[str, str],
) -> ViewItem:
    key = item_lookup_key(item, index)
    if not isinstance(item, dict):
        return ViewItem(key, "user", str(item), None, item, event_ids.get(key))
    call_id = str(item.get("call_id") or "")
    text = str(item.get("output") or "")
    if not isinstance(item.get("output"), str):
        text = _content_text(item.get("output"))
    return ViewItem(
        key, "function_call_output", text, call_id or None, item, event_ids.get(key),
        tool_name=names.get(call_id) or str(item.get("name") or ""),
    )


def item_lookup_key(item: Any, index: int) -> str:
    """Stable key for one model-input item. Indices of earlier items do not move."""
    if isinstance(item, dict):
        kind = str(item.get("type") or item.get("role") or "user")
        call_id = item.get("call_id")
        if kind in {
            "function_call", "function_call_output",
            "custom_tool_call", "custom_tool_call_output",
        }:
            return f"{kind}:{call_id or index}"
        if kind == "reasoning":
            return f"reasoning:{index}:{_reasoning_text(item)[:40]}"
        role = str(item.get("role") or "user")
        text = _content_text(item.get("content"))
        mapped = "assistant" if role == "assistant" else "user"
        if text.startswith("Summary of forgotten context:\n") or item.get("kind") == "summary":
            mapped = "summary"
        return f"{mapped}:{index}:{text[:80]}"
    return f"user:{index}:{str(item)[:80]}"


def _item_to_view(item: Any, index: int, event_ids: dict[str, str]) -> ViewItem:
    key = item_lookup_key(item, index)
    if isinstance(item, dict):
        kind = str(item.get("type") or item.get("role") or "user")
        call_id = item.get("call_id")
        if kind in {
            "function_call", "function_call_output",
            "custom_tool_call", "custom_tool_call_output",
        }:
            text = str(item.get("arguments") or item.get("input") or item.get("output") or "")
            return ViewItem(key, kind, text, str(call_id) if call_id else None, item,
                            event_ids.get(key))
        if kind == "reasoning":
            text = _reasoning_text(item)
            return ViewItem(key, "reasoning", text, None, item, event_ids.get(key))
        role = str(item.get("role") or "user")
        text = _content_text(item.get("content"))
        mapped = "assistant" if role == "assistant" else "user"
        if text.startswith("Summary of forgotten context:\n") or item.get("kind") == "summary":
            mapped = "summary"
        return ViewItem(key, mapped, text, None, item, event_ids.get(key))
    return ViewItem(key, "user", str(item), None, item, event_ids.get(key))


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
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _reasoning_text(item: dict[str, Any]) -> str:
    parts = []
    for block in item.get("summary") or item.get("content") or []:
        if isinstance(block, dict):
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(part for part in parts if part)


def _apply_record(view: list[ViewItem], record: CondensationRecord) -> list[ViewItem]:
    remaining = [item for item in view if item.key not in record.forgotten_keys]
    summary = ViewItem(
        key=f"summary:{record.summary[:80]}",
        kind="summary",
        text=record.summary,
        raw={"role": "user", "content": record.summary, "kind": "summary"},
    )
    offset = min(record.summary_offset, len(remaining))
    remaining.insert(offset, summary)
    return remaining


def _instructions_of(view: list[ViewItem]) -> str | None:
    for item in view:
        if item.kind == "system":
            return item.text
    return None


def _input_of(view: list[ViewItem]) -> list[Any]:
    rendered: list[Any] = []
    for item in view:
        if item.kind == "system":
            continue
        rendered.extend(item.prefix_raw)
        if item.kind == "summary":
            rendered.append({"role": "user", "content": item.text})
            continue
        if item.raw is not None and item.kind != "system":
            rendered.append(item.raw)
            continue
        role = "assistant" if item.kind == "assistant" else "user"
        rendered.append({"role": role, "content": item.text})
    return rendered


def _manipulation_indices(view: list[ViewItem]) -> set[int]:
    """Cuts that do not fall between a tool call and its result."""
    indices = set(range(len(view) + 1))
    pending: set[str] = set()
    for index, item in enumerate(view):
        if (
            index > 0
            and item.kind == "function_call"
            and view[index - 1].kind == "function_call"
            and item.batch_id
            and item.batch_id == view[index - 1].batch_id
        ):
            # Parallel calls from one response are one batch. 1.49.2
            # BatchAtomicityProperty removes the cut between them.
            indices.discard(index)
        if item.kind == "function_call" and item.call_id:
            pending.add(item.call_id)
        elif item.kind == "function_call_output" and item.call_id:
            pending.discard(item.call_id)
        if pending:
            indices.discard(index + 1)
    return indices


def _find_next(indices: set[int], threshold: int) -> int:
    valid = [index for index in indices if index >= threshold]
    if not valid:
        raise ValueError(f"no manipulation index >= {threshold}")
    return min(valid)


def _forgotten_range(
    view: list[ViewItem],
    *,
    keep_first: int,
    max_tokens: int,
    count: Callable[[list[ViewItem]], int],
) -> tuple[list[ViewItem], int]:
    total = count(view)
    tokens_to_reduce = total - (max_tokens // 2)
    head = view[:keep_first]
    tail = view[keep_first:]
    suffix = _suffix_length(tail, head, tokens_to_reduce, count)
    naive_end = len(view) - suffix
    indices = _manipulation_indices(view)
    start = _find_next(indices, keep_first)
    end = _find_next(indices, naive_end)
    if end < start:
        end = start
    return view[start:end], start


def _suffix_length(
    events: list[ViewItem],
    base: list[ViewItem],
    token_reduction: int,
    count: Callable[[list[ViewItem]], int],
) -> int:
    if not events or token_reduction <= 0:
        return len(events)
    base_tokens = count(base) if base else 0

    def region(prefix: int) -> int:
        return count([*base, *events[:prefix]]) - base_tokens

    if region(len(events)) <= token_reduction:
        return 0
    left, right = 1, len(events)
    while left < right:
        mid = (left + right) // 2
        if region(mid) > token_reduction:
            right = mid
        else:
            left = mid + 1
    return len(events) - left
