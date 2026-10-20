"""OpenHands-shaped events the shared trajectory recorder already understands.

Class names match what ``record_openhands_events`` and
``context_management_record`` branch on. ``record_openhands_events`` leaves a
condensation out of the judge-facing steps. ``place_condensation_steps``
inserts it afterwards, at the trajectory step where it took effect.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


class _Text:
    def __init__(self, text: str):
        self.text = text


class MessageEvent:
    def __init__(self, *, source: str, text: str, id: str, timestamp: str | None = None):
        self.source = source
        self.id = id
        self.timestamp = timestamp
        self.llm_message = SimpleNamespace(content=[_Text(text)])
        self._text = text

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "MessageEvent", "source": self.source, "id": self.id, "text": self._text}


class ActionEvent:
    def __init__(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        id: str,
        timestamp: str | None = None,
    ):
        self.tool_name = tool_name
        self.id = id
        self.tool_call_id = id
        self.timestamp = timestamp
        self.action = SimpleNamespace(data=dict(arguments))
        self._arguments = dict(arguments)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "ActionEvent",
            "tool_name": self.tool_name,
            "id": self.id,
            "arguments": self._arguments,
        }


class ObservationEvent:
    def __init__(
        self,
        *,
        tool_name: str,
        text: str,
        action_id: str,
        timestamp: str | None = None,
    ):
        self.tool_name = tool_name
        self.action_id = action_id
        self.tool_call_id = action_id
        self.id = f"{action_id}-obs"
        self.timestamp = timestamp
        self.observation = SimpleNamespace(content=[_Text(text)])
        self._text = text

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "ObservationEvent",
            "tool_name": self.tool_name,
            "action_id": self.action_id,
            "text": self._text,
        }


class SystemPromptEvent:
    """The sealed prompt. With keep_first=0 the first condensation forgets it."""

    def __init__(self, *, text: str, id: str = "evt-system", timestamp: str | None = None):
        self.id = id
        self.text = text
        self.timestamp = timestamp

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "SystemPromptEvent", "id": self.id, "text": self.text}


class Condensation:
    def __init__(
        self,
        *,
        id: str,
        forgotten_event_ids: list[str],
        summary: str,
        summary_offset: int,
        timestamp: str | None = None,
        trigger_tokens: int | None = None,
        previous_summary_forgotten: bool = False,
    ):
        self.id = id
        self.forgotten_event_ids = list(forgotten_event_ids)
        self.summary = summary
        self.summary_offset = summary_offset
        self.timestamp = timestamp
        self.trigger_tokens = trigger_tokens
        self.previous_summary_forgotten = previous_summary_forgotten

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "Condensation",
            "id": self.id,
            "forgotten_event_ids": self.forgotten_event_ids,
            "summary": self.summary,
            "summary_offset": self.summary_offset,
            "trigger_tokens": self.trigger_tokens,
            "previous_summary_forgotten": self.previous_summary_forgotten,
        }


def mcp_observation(tool_name: str, payload: Any) -> str:
    """The prefix OpenHands puts on a business-tool observation."""
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, default=str)
    if body.startswith("[Tool "):
        return body
    return f"[Tool '{tool_name}' executed.]\n{body}"
