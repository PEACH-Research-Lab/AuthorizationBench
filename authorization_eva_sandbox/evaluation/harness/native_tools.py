"""Map a harness's own shell and file tools onto the steps the judges read.

SandboxAgent's ``exec_command`` and ``write_stdin`` and Claude Code's ``Bash``
become ``terminal_command`` steps (command, output, exit_code). Claude Code's
``Read`` becomes ``file_read``. ``apply_patch``, Claude Code's ``Write``,
``Edit`` and ``MultiEdit`` become ``file_edit`` steps with the fields
``post_hoc_eval`` and the digest already show: ``file_text`` for a whole
file, ``old_str`` / ``new_str`` for a replacement, plus ``patch`` for an
``apply_patch`` section. Every mapped step carries ``native_tool`` so side
effect checks can tell native tools from ours.

A patch's resulting file text is recorded when it can be derived: an added
file from its ``+`` lines, an updated file from ``files_after`` (read by the
adapter after the call) or by applying the hunks to ``files_before``.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any, Mapping

from authorization_eva_sandbox.evaluation.harness.openhands_events import (
    _capped_text,
    file_edit_fields,
)

WORKSPACE_ROOT = "/workspace"

SANDBOX_TERMINAL_TOOLS = frozenset({"exec_command", "write_stdin"})
CLAUDE_TERMINAL_TOOLS = frozenset({"Bash"})
CLAUDE_FILE_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit"})
PATCH_TOOLS = frozenset({"apply_patch"})
NATIVE_TOOL_NAMES = (
    SANDBOX_TERMINAL_TOOLS | CLAUDE_TERMINAL_TOOLS | CLAUDE_FILE_TOOLS | PATCH_TOOLS
)

_SANDBOX_EXIT = re.compile(r"^Process exited with code (-?\d+)$", re.M)
_SANDBOX_SESSION = re.compile(r"^Process running with session ID (\d+)$", re.M)
_CLAUDE_EXIT = re.compile(r"^(?:Error: )?Exit code (-?\d+)\b", re.M)

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File: "
_DELETE = "*** Delete File: "
_UPDATE = "*** Update File: "
_MOVE = "*** Move to: "
_EOF = "*** End of File"


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
        return parsed if isinstance(parsed, dict) else {"input": value}
    return {}


def absolute_path(path: str, root: str = WORKSPACE_ROOT) -> str:
    if not path:
        return ""
    if path.startswith("/"):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(root, path))


def native_steps(
    tool: str,
    arguments: Any,
    output: str = "",
    *,
    is_error: bool | None = None,
    root: str = WORKSPACE_ROOT,
    files_before: Mapping[str, str] | None = None,
    files_after: Mapping[str, str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """``(step type, data)`` pairs for one native tool call and its result."""
    args = _arguments(arguments)
    output = "" if output is None else str(output)
    if tool == "exec_command":
        return [("terminal_command", _exec_command(args, output))]
    if tool == "write_stdin":
        return [("terminal_command", _write_stdin(args, output))]
    if tool == "Bash":
        return [("terminal_command", _bash(args, output, is_error))]
    if tool == "Read":
        path = absolute_path(str(args.get("file_path") or args.get("path") or ""), root)
        data = {"path": path, "result": output, "native_tool": tool}
        for key in ("offset", "limit"):
            if args.get(key) is not None:
                data[key] = args[key]
        return [("file_read", data)]
    if tool in {"Write", "Edit", "MultiEdit"}:
        return _claude_edit(tool, args, output, root, files_after)
    if tool == "apply_patch":
        return apply_patch_steps(
            args, output, root=root, files_before=files_before, files_after=files_after,
        )
    raise ValueError(f"not a native tool: {tool!r}")


def record_native_steps(trajectory: Any, tool: str, arguments: Any, output: str = "", **kwargs: Any) -> list[Any]:
    """Append the mapped steps to a ``TrajectoryLog``."""
    return [
        trajectory.add_step(kind, **data)
        for kind, data in native_steps(tool, arguments, output, **kwargs)
    ]


def _exit_code(pattern: re.Pattern[str], output: str) -> int | None:
    match = pattern.search(output)
    return int(match.group(1)) if match else None


def _exec_command(args: dict[str, Any], output: str) -> dict[str, Any]:
    cmd = str(args.get("cmd") or args.get("command") or "")
    workdir = args.get("workdir")
    # The SDK runs ``cd <workdir> && <cmd>``; the step records the same line.
    command = f"cd {shlex.quote(str(workdir))} && {cmd}" if workdir else cmd
    data: dict[str, Any] = {
        "command": command,
        "output": output,
        "exit_code": _exit_code(_SANDBOX_EXIT, output),
        "native_tool": "exec_command",
        "cmd": cmd,
    }
    if workdir:
        data["workdir"] = str(workdir)
    if args.get("tty"):
        data["tty"] = True
    session = _exit_code(_SANDBOX_SESSION, output)
    if session is not None:
        data["session_id"] = session
    return data


def _write_stdin(args: dict[str, Any], output: str) -> dict[str, Any]:
    return {
        "command": str(args.get("chars") or ""),
        "output": output,
        "exit_code": _exit_code(_SANDBOX_EXIT, output),
        "native_tool": "write_stdin",
        "stdin": True,
        "session_id": args.get("session_id"),
    }


def _bash(args: dict[str, Any], output: str, is_error: bool | None) -> dict[str, Any]:
    code = _exit_code(_CLAUDE_EXIT, output)
    if code is None and is_error is False:
        code = 0
    data: dict[str, Any] = {
        "command": str(args.get("command") or ""),
        "output": output,
        "exit_code": code,
        "native_tool": "Bash",
    }
    if is_error is not None:
        data["is_error"] = bool(is_error)
    if args.get("run_in_background"):
        data["run_in_background"] = True
    if args.get("description"):
        data["description"] = str(args["description"])
    return data


def _claude_edit(
    tool: str,
    args: dict[str, Any],
    output: str,
    root: str,
    files_after: Mapping[str, str] | None,
) -> list[tuple[str, dict[str, Any]]]:
    path = absolute_path(str(args.get("file_path") or args.get("path") or ""), root)
    if tool == "Write":
        edits = [file_edit_fields("write_file", {"content": args.get("content")})]
    elif tool == "Edit":
        edits = [_replacement(args)]
    else:
        edits = [_replacement(edit) for edit in args.get("edits") or [] if isinstance(edit, dict)]
    after = _lookup(files_after, path, root)
    steps = []
    for fields in edits:
        data = {"path": path, "result": output, "native_tool": tool, **fields}
        if after is not None and "file_text" not in data:
            data["file_text"], cut = _capped_text(after)
            data["content_truncated"] = bool(data.get("content_truncated")) or cut
        steps.append(("file_edit", data))
    return steps


def _replacement(args: dict[str, Any]) -> dict[str, Any]:
    """An empty ``old_string`` creates the file, as in Claude Code."""
    fields = file_edit_fields("edit", {
        "old_string": args.get("old_string"),
        "new_string": args.get("new_string"),
    })
    if args.get("replace_all"):
        fields["replace_all"] = True
    return fields


def _lookup(files: Mapping[str, str] | None, path: str, root: str) -> str | None:
    if not files:
        return None
    for key, text in files.items():
        if absolute_path(str(key), root) == path:
            return text
    return None


def patch_text(args: Any) -> str | None:
    """The freeform patch in an ``apply_patch`` call, if it is one."""
    if isinstance(args, str):
        return args
    for key in ("input", "patch"):
        value = args.get(key) if isinstance(args, dict) else None
        if isinstance(value, str):
            return value
    return None


def parse_patch(text: str) -> list[dict[str, Any]]:
    """Sections of a ``*** Begin Patch`` envelope: op, path, move_to, body lines."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line in (_BEGIN, _END):
            current = None
            continue
        header = next(
            ((op, line[len(prefix):].strip()) for op, prefix in (
                ("add", _ADD), ("delete", _DELETE), ("update", _UPDATE),
            ) if line.startswith(prefix)),
            None,
        )
        if header is not None:
            current = {"op": header[0], "path": header[1], "move_to": None, "lines": [], "header": line}
            sections.append(current)
            continue
        if current is None:
            continue
        if line.startswith(_MOVE) and current["op"] == "update" and not current["lines"]:
            current["move_to"] = line[len(_MOVE):].strip()
            continue
        current["lines"].append(line)
    return sections


def _operations(args: dict[str, Any]) -> list[dict[str, Any]]:
    """Patch sections from a freeform patch or from Responses ``apply_patch_call`` operations."""
    text = patch_text(args)
    if text is not None:
        return parse_patch(text)
    raw = args.get("operations") or args.get("operation") or args
    operations = raw if isinstance(raw, list) else [raw]
    out = []
    for operation in operations:
        if not isinstance(operation, dict) or not operation.get("path"):
            continue
        kind = str(operation.get("type") or "")
        op = {"create_file": "add", "update_file": "update", "delete_file": "delete"}.get(kind)
        if op is None:
            continue
        diff = str(operation.get("diff") or "")
        out.append({
            "op": op,
            "path": str(operation["path"]),
            "move_to": operation.get("move_to"),
            "lines": diff.splitlines(),
            "header": f"{kind} {operation['path']}",
        })
    return out


def _hunks(lines: list[str]) -> list[tuple[list[str], list[str]]]:
    """``(old lines, new lines)`` per ``@@`` hunk of an update section."""
    hunks: list[tuple[list[str], list[str]]] = []
    old: list[str] = []
    new: list[str] = []

    def close() -> None:
        if old or new:
            hunks.append((list(old), list(new)))
        old.clear()
        new.clear()

    for line in lines:
        if line.startswith("@@"):
            close()
            continue
        if line == _EOF:
            continue
        marker, body = (line[:1], line[1:]) if line else (" ", "")
        if marker == "-":
            old.append(body)
        elif marker == "+":
            new.append(body)
        else:
            old.append(body)
            new.append(body)
    close()
    return hunks


def apply_hunks(before: str, hunks: list[tuple[list[str], list[str]]]) -> str | None:
    """Apply update hunks in order, each at its first match after the previous one."""
    lines = before.split("\n")
    cursor = 0
    for old, new in hunks:
        at = _find(lines, old, cursor)
        if at is None:
            return None
        lines[at:at + len(old)] = new
        cursor = at + len(new)
    return "\n".join(lines)


def _find(lines: list[str], block: list[str], start: int) -> int | None:
    if not block:
        return start
    for index in range(start, len(lines) - len(block) + 1):
        if lines[index:index + len(block)] == block:
            return index
    stripped = [item.rstrip() for item in block]
    for index in range(start, len(lines) - len(block) + 1):
        if [item.rstrip() for item in lines[index:index + len(block)]] == stripped:
            return index
    return None


def apply_patch_steps(
    arguments: Any,
    output: str = "",
    *,
    root: str = WORKSPACE_ROOT,
    files_before: Mapping[str, str] | None = None,
    files_after: Mapping[str, str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """One ``file_edit`` per file the patch touches."""
    args = _arguments(arguments) if not isinstance(arguments, str) else {"input": arguments}
    steps = []
    for section in _operations(args):
        path = absolute_path(section["path"], root)
        body = "\n".join([section["header"], *section["lines"]])
        patch, patch_cut = _capped_text(body)
        data: dict[str, Any] = {
            "path": path,
            "result": output,
            "native_tool": "apply_patch",
            "patch": patch,
            "content_truncated": patch_cut,
        }
        if section["op"] == "add":
            text = "\n".join(line[1:] for line in section["lines"] if line.startswith("+"))
            data["command"] = "create"
            data["file_text"], cut = _capped_text(text)
            data["content_truncated"] = patch_cut or cut
        elif section["op"] == "delete":
            data["command"] = "delete"
        else:
            data["command"] = "apply_patch"
            if section.get("move_to"):
                data["move_to"] = absolute_path(str(section["move_to"]), root)
            hunks = _hunks(section["lines"])
            old = "\n...\n".join("\n".join(item[0]) for item in hunks)
            new = "\n...\n".join("\n".join(item[1]) for item in hunks)
            data["old_str"], old_cut = _capped_text(old)
            data["new_str"], new_cut = _capped_text(new)
            after = _lookup(files_after, data.get("move_to") or path, root)
            if after is None:
                before = _lookup(files_before, path, root)
                after = apply_hunks(before, hunks) if before is not None else None
            cut = False
            if after is not None:
                data["file_text"], cut = _capped_text(after)
            data["content_truncated"] = patch_cut or old_cut or new_cut or cut
        steps.append(("file_edit", data))
    return steps
