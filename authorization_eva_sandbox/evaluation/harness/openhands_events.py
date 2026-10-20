"""Map OpenHands SDK events onto the trajectory a judge already reads."""
from __future__ import annotations

import json
from typing import Any


# Written file text kept on a file_edit step. A normal report fits; larger
# bodies set content_truncated and keep this prefix.
FILE_EDIT_CONTENT_CAP = 200_000

# SDK 1.49.2 file tools. write_file takes content; file_editor takes command
# plus file_text / old_str / new_str; edit (Gemini) takes old_string / new_string.
_FILE_EDIT_TOOLS = {"write_file", "file_edit", "file_editor", "edit", "planning_file_editor"}


def _capped_text(value: Any) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    if len(text) <= FILE_EDIT_CONTENT_CAP:
        return text, False
    return text[:FILE_EDIT_CONTENT_CAP], True


def _present(arguments: dict, *names: str) -> Any:
    for name in names:
        if name in arguments and arguments[name] is not None:
            return arguments[name]
    return None


def file_edit_fields(tool: str, arguments: dict) -> dict:
    """Content recorded on a file_edit step.

    A write or create stores the full new file text as ``file_text``.
    A str_replace-style edit stores ``old_str`` and ``new_str``.
    """
    command = str(arguments.get("command") or "")
    old = _present(arguments, "old_str", "old_string")
    new = _present(arguments, "new_str", "new_string")
    file_text = _present(arguments, "file_text")
    content = _present(arguments, "content")
    truncated = False
    fields: dict[str, Any] = {}

    replacing = command in {"str_replace", "insert", "undo_edit"} or (
        tool == "edit" and old not in (None, "")
    )
    if replacing and not (command in {"", "write", "create"} and content is not None):
        fields["command"] = command or "str_replace"
        if old is not None:
            clipped, cut = _capped_text(old)
            fields["old_str"] = clipped
            truncated = truncated or cut
        if new is not None:
            clipped, cut = _capped_text(new)
            fields["new_str"] = clipped
            truncated = truncated or cut
        if arguments.get("insert_line") is not None:
            fields["insert_line"] = arguments.get("insert_line")
    else:
        body = file_text if file_text is not None else content
        if body is None and new is not None and (command == "create" or tool == "edit"):
            body = new
        clipped, cut = _capped_text("" if body is None else body)
        fields["file_text"] = clipped
        truncated = cut
        if command == "create" or (tool == "edit" and old == ""):
            fields["command"] = "create"
        else:
            fields["command"] = command or "write"
    fields["content_truncated"] = truncated
    return fields


def _action_data(event: Any) -> dict:
    action = getattr(event, "action", None)
    data = getattr(action, "data", None)
    if isinstance(data, dict):
        return data
    dump = getattr(action, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, dict):
            return dumped
    message = getattr(action, "message", None)
    if isinstance(message, str):
        return {"message": message}
    return {}


def _observation_text(event: Any) -> str:
    observation = getattr(event, "observation", None)
    content = getattr(observation, "content", None) or []
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _json_object(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _event_kind(event: Any) -> str:
    name = type(event).__name__
    if name in {"ActionEvent", "ObservationEvent", "MessageEvent"}:
        return name
    if getattr(event, "llm_message", None) is not None:
        return "MessageEvent"
    if getattr(event, "observation", None) is not None:
        return "ObservationEvent"
    if getattr(event, "tool_name", None):
        return "ActionEvent"
    return name


def record_openhands_events(
    trajectory: Any,
    events: list[Any],
    *,
    tool_names: Any = None,
) -> None:
    """Append judge-facing steps for one SDK conversation.

    ``message_user`` becomes ``ask_user``. The finish tool's message becomes
    ``agent_finish.final_thought``. Other tool calls become ``tool_call``.
    ``tool_names`` restores a published identifier name to the canonical
    ``service.operation`` name before the step is recorded. Another harness's
    own shell and file tools (``native_tools.NATIVE_TOOL_NAMES``) become
    ``terminal_command``, ``file_read`` and ``file_edit`` steps.

    A model may issue several tool calls before any result comes back, so an
    observation is paired with its action by ``action_id`` / ``tool_call_id``
    (by tool name, first unanswered, when the events carry no ids). Steps are
    recorded in the order the actions and messages were emitted.
    """
    def canonical(name: str) -> str:
        return tool_names.canonical(name) if tool_names is not None and name else name

    items: list[tuple[str, Any]] = []
    by_id: dict[str, dict] = {}
    for event in events:
        kind = _event_kind(event)
        if kind == "MessageEvent":
            message = getattr(event, "llm_message", None)
            content = getattr(message, "content", None) or []
            texts = [getattr(block, "text", "") for block in content]
            text = "\n".join(part for part in texts if part).strip()
            if text:
                step_type = (
                    "agent_message"
                    if getattr(event, "source", None) == "agent"
                    else "user_message"
                )
                items.append(("message", (step_type, text)))
            continue
        if kind == "ObservationEvent":
            entry = None
            for key in (getattr(event, "action_id", None), getattr(event, "tool_call_id", None)):
                if isinstance(key, str) and key in by_id and by_id[key]["observation"] is None:
                    entry = by_id[key]
                    break
            if entry is None:
                observed = canonical(str(getattr(event, "tool_name", "") or ""))
                entry = next((e for kind_, e in items if kind_ == "action"
                              and e["observation"] is None and e["tool"] != "finish"
                              and (not observed or e["tool"] == observed)), None)
            if entry is not None:
                entry["observation"] = event
            continue
        if kind != "ActionEvent":
            continue
        entry = {"tool": canonical(str(getattr(event, "tool_name", "") or "")),
                 "arguments": _action_data(event), "observation": None,
                 "files_before": getattr(event, "files_before", None)}
        items.append(("action", entry))
        for key in (getattr(event, "id", None), getattr(event, "tool_call_id", None)):
            if isinstance(key, str) and key:
                by_id[key] = entry
    for kind, item in items:
        if kind == "message":
            trajectory.add_step(item[0], content=item[1])
        else:
            _complete_action(trajectory, item, item["observation"])


def _complete_action(trajectory: Any, pending: dict, observation: Any | None) -> None:
    tool = pending["tool"]
    arguments = pending["arguments"]
    body = _observation_text(observation) if observation is not None else ""
    payload = _json_object(body)
    if tool == "finish":
        trajectory.add_step(
            "agent_finish",
            final_thought=str(arguments.get("message") or ""),
            outputs="",
        )
        return
    if tool == "think":
        trajectory.add_step(
            "agent_think",
            thought=str(arguments.get("thought") or arguments.get("message") or ""),
        )
        return
    if tool in {"browse", "browse_interactive", "browser"}:
        trajectory.add_browser_action(
            action=str(arguments.get("browser_actions") or arguments.get("action") or tool),
            url=str(arguments.get("url") or ""),
            element=str(arguments.get("focused_element_bid") or arguments.get("element") or ""),
        )
        return
    if tool in {"run_ipython", "ipython"}:
        code = str(arguments.get("code") or "")
        trajectory.add_step("ipython_cell", code=code)
        if body:
            trajectory.add_step("ipython_output", code=code, output=body)
        return
    if tool == "message_user":
        reply = None
        responded = False
        inner = payload.get("body") if isinstance(payload.get("body"), dict) else payload
        if isinstance(inner, dict):
            reply = inner.get("reply")
            responded = bool(inner.get("responded"))
        trajectory.add_ask_user(
            str(arguments.get("message") or ""),
            reply if isinstance(reply, str) else None,
            responded=responded,
        )
        return
    from authorization_eva_sandbox.evaluation.harness.native_tools import (
        NATIVE_TOOL_NAMES,
        record_native_steps,
    )

    if tool in NATIVE_TOOL_NAMES:
        record_native_steps(
            trajectory, tool, arguments, body,
            is_error=getattr(observation, "is_error", None),
            files_before=pending.get("files_before"),
            files_after=getattr(observation, "files_after", None),
        )
        return
    # The SDK file tools name the argument file_path.
    path = str(arguments.get("path") or arguments.get("file_path") or "")
    if tool in {"read_file", "file_read"}:
        trajectory.add_step("file_read", path=path, result=body)
        return
    if tool in _FILE_EDIT_TOOLS:
        if str(arguments.get("command") or "") == "view":
            trajectory.add_step("file_read", path=path, result=body)
            return
        trajectory.add_step(
            "file_edit",
            path=path,
            result=body,
            **file_edit_fields(tool, arguments),
        )
        return
    if tool in {"bash", "terminal"}:
        trajectory.add_terminal_command(
            str(arguments.get("command") or ""),
            output=body,
        )
        return
    service, _, name = tool.partition(".")
    trajectory.add_step(
        "tool_call",
        tool=name or tool,
        service=service if name else "",
        arguments=arguments,
        status=payload.get("status", ""),
        result=body,
    )
