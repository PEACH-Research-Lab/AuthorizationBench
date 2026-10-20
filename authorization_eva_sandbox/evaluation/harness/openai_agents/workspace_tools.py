"""Shell and file tools. Schemas come from the exported OpenHands specs.

``terminal`` is one persistent bash in the container (cwd, env, functions,
and background jobs survive). A command with no ``timeout`` pauses after 30
seconds without new output; ``timeout`` is a hard limit and disables that
pause. ``read_file`` and ``write_file`` stay single ``docker exec`` calls.
``read_file`` pages 1_000 lines at a time. Output is clipped at 30_000
characters, head and tail kept.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

SPEC_PATH = Path(__file__).with_name("openhands_tool_specs.json")
MAX_CMD_OUTPUT_SIZE = 30_000
NO_CHANGE_TIMEOUT_SECONDS = 30
MAX_LINES_PER_READ = 1000
# openhands/tools/terminal/constants.py TIMEOUT_MESSAGE_TEMPLATE
TIMEOUT_MESSAGE_TEMPLATE = (
    "You may wait longer to see additional output by sending empty command '', "
    "send other commands to interact with the current process, send keys "
    '("C-c", "C-z", "C-d") '
    "to interrupt/kill the previous command before sending your new command, "
    "or use the timeout parameter in terminal for future commands."
)
_TRUNCATE_NOTICE = (
    "<response clipped><NOTE>Due to the max output limit, only part of the full "
    "response has been shown to you.</NOTE>"
)
_META_ARGUMENTS = {"security_risk", "summary"}

Exec = Callable[..., subprocess.CompletedProcess[str]]


def load_spec_document() -> dict[str, Any]:
    """The exported 1.49.2 tool document. Older files were a bare list of tools."""
    data = json.loads(SPEC_PATH.read_text())
    if isinstance(data, list):
        return {"tools": data}
    return data


def load_tool_specs() -> dict[str, dict[str, Any]]:
    return {spec["name"]: spec for spec in load_spec_document()["tools"]}


def clip_output(content: str, limit: int = MAX_CMD_OUTPUT_SIZE) -> str:
    """Middle-truncate like OpenHands ``maybe_truncate``."""
    if not limit or len(content) <= limit:
        return content
    if len(_TRUNCATE_NOTICE) >= limit:
        return _TRUNCATE_NOTICE[:limit]
    remaining = limit - len(_TRUNCATE_NOTICE)
    head = remaining // 2 + remaining % 2
    tail = remaining - head
    return content[:head] + _TRUNCATE_NOTICE + (content[-tail:] if tail else "")


def business_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Ignore OpenHands prediction fields if a model still sends them.

    They are not in the schemas this harness publishes, and they are not required.
    """
    return {key: value for key, value in arguments.items() if key not in _META_ARGUMENTS}


class WorkspaceTools:
    """``terminal``, ``read_file``, ``write_file``, and ``finish`` for one container."""

    def __init__(
        self,
        container_name: str,
        *,
        user: str,
        cwd: str,
        exec_fn: Exec | None = None,
        shell_argv: list[str] | None = None,
        no_change_timeout_seconds: float | None = None,
    ):
        self.container_name = container_name
        self.user = user
        self.home = cwd
        self.cwd = cwd
        self._exec = exec_fn
        self._run_exec = exec_fn or subprocess.run
        self.shell_argv = shell_argv
        self.no_change_timeout_seconds = (
            NO_CHANGE_TIMEOUT_SECONDS
            if no_change_timeout_seconds is None
            else no_change_timeout_seconds
        )
        self._session: Any = None
        self.finished = False
        self.finish_message = ""

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.close()

    def terminal(self, arguments: dict[str, Any]) -> str:
        if self._exec is not None:
            return self._terminal_oneshot(arguments)
        session = self._ensure_session()
        timeout = arguments.get("timeout")
        result = session.execute(
            str(arguments.get("command") or ""),
            timeout=float(timeout) if timeout else None,
            is_input=bool(arguments.get("is_input")),
            reset=bool(arguments.get("reset")),
        )
        self.cwd = result.cwd
        return result.text

    def _ensure_session(self) -> Any:
        if self._session is None:
            from authorization_eva_sandbox.evaluation.harness.openai_agents.terminal_session import (
                PersistentBash,
            )

            argv = self.shell_argv or [
                "docker", "exec", "-i", "-t",
                "-u", self.user,
                "-w", self.home,
                self.container_name,
                "bash", "--noprofile", "--norc", "-i",
            ]
            self._session = PersistentBash(
                argv,
                cwd=self.home,
                no_change_timeout_seconds=self.no_change_timeout_seconds,
                host_cwd=self.home if self.shell_argv else None,
            )
        return self._session

    def _terminal_oneshot(self, arguments: dict[str, Any]) -> str:
        """Test double: one exec per call, used when ``exec_fn`` is injected."""
        command = str(arguments.get("command") or "")
        if arguments.get("reset"):
            self.cwd = self.home
        timeout = arguments.get("timeout")
        limit = float(timeout) if timeout else float(self.no_change_timeout_seconds)
        script = (
            'cd "$AB_CWD" || exit 1\n'
            'eval "$AB_CMD"\n'
            '__ab_status=$?\n'
            'printf "\\n__AB_CWD__%s\\n" "$PWD"\n'
            'exit $__ab_status\n'
        )
        proc, timed_out = self._run(
            ["bash", "-lc", script],
            timeout=limit,
            extra_env={"AB_CWD": self.cwd, "AB_CMD": command},
        )
        output = f"{proc.stdout or ''}{proc.stderr or ''}"
        marker = "\n__AB_CWD__"
        if marker in output:
            output, _, cwd = output.rpartition(marker)
            self.cwd = cwd.strip().splitlines()[0] if cwd.strip() else self.cwd
        code = -1 if timed_out else int(proc.returncode)
        if timed_out:
            output += (
                "\n[The command timed out after "
                f"{limit:g} seconds. {TIMEOUT_MESSAGE_TEMPLATE}]"
            )
        else:
            output += f"\n[The command completed with exit code {code}.]"
        output += f"\n[Current working directory: {self.cwd}]"
        if code != -1:
            output += f"\n[Command finished with exit code {code}]"
        return clip_output(output)

    def read_file(self, arguments: dict[str, Any]) -> str:
        path = self._resolve(str(arguments.get("file_path") or arguments.get("path") or ""))
        proc, timed_out = self._run(["bash", "-lc", "cat -- \"$AB_PATH\""], timeout=30,
                                    extra_env={"AB_PATH": path})
        if timed_out:
            return f"Error reading file: timed out: {path}"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "unable to read").strip()
            return f"Error reading file: {err}"
        lines = (proc.stdout or "").splitlines(keepends=True)
        total = len(lines)
        offset = int(arguments.get("offset") or 0)
        if offset >= total and total > 0:
            return f"Error: Offset {offset} is beyond file length ({total} lines)"
        limit = arguments.get("limit")
        start = offset
        end = min(start + int(limit), total) if limit else min(start + MAX_LINES_PER_READ, total)
        numbered = [f"{i:6d}  {line}" for i, line in enumerate(lines[start:end], start=start + 1)]
        body = "".join(numbered)
        header = f"Read file: {path}"
        if end < total:
            header += (
                f" (showing lines {start + 1}-{end} of {total}) "
                f"To read more, use: read_file(file_path='{path}', "
                f"offset={end}, limit={limit or MAX_LINES_PER_READ})"
            )
        return clip_output(header + "\n\n" + body)

    def write_file(self, arguments: dict[str, Any]) -> str:
        path = self._resolve(str(arguments.get("file_path") or arguments.get("path") or ""))
        content = "" if arguments.get("content") is None else str(arguments.get("content"))
        exists, _ = self._run(
            ["bash", "-lc", 'if [ -d "$AB_PATH" ]; then echo dir; elif [ -e "$AB_PATH" ]; then echo file; else echo new; fi'],
            timeout=15,
            extra_env={"AB_PATH": path},
        )
        kind = (exists.stdout or "").strip()
        if kind == "dir":
            return f"Error: Path is a directory, not a file: {path}"
        proc, timed_out = self._run(
            ["bash", "-lc", 'mkdir -p "$(dirname "$AB_PATH")" && cat > "$AB_PATH"'],
            timeout=30,
            extra_env={"AB_PATH": path},
            stdin=content,
        )
        if timed_out:
            return f"Error writing file: timed out: {path}"
        if proc.returncode != 0:
            return f"Error writing file: {(proc.stderr or 'write failed').strip()}"
        verb = "Created" if kind == "new" else "Updated"
        return f"{verb} file: {path}"

    def finish(self, arguments: dict[str, Any]) -> str:
        self.finished = True
        self.finish_message = str(arguments.get("message") or "")
        return self.finish_message

    def _resolve(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return str(Path(self.cwd) / path)

    def _run(
        self,
        argv: list[str],
        *,
        timeout: float,
        extra_env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], bool]:
        command = [
            "docker", "exec", "-i",
            "-u", self.user,
            "-w", self.cwd,
        ]
        for key, value in (extra_env or {}).items():
            command.extend(["-e", f"{key}={value}"])
        command.append(self.container_name)
        command.extend(argv)
        try:
            proc = self._run_exec(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc, False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            return subprocess.CompletedProcess(command, 124, stdout, stderr), True


def function_tools(names: list[str], workspace: WorkspaceTools) -> list[Any]:
    """Function tools whose name, description, and parameters are the export."""
    from agents.tool import FunctionTool

    specs = load_tool_specs()
    handlers = {
        "terminal": workspace.terminal,
        "read_file": workspace.read_file,
        "write_file": workspace.write_file,
        "finish": workspace.finish,
    }
    tools = []
    for name in names:
        spec = specs[name]
        handler = handlers[name]

        async def _invoke(ctx: Any, raw: str, _handler=handler) -> str:
            del ctx
            try:
                arguments = json.loads(raw or "{}")
            except json.JSONDecodeError:
                return "Error: tool arguments were not valid JSON"
            if not isinstance(arguments, dict):
                return "Error: tool arguments must be an object"
            return _handler(business_arguments(arguments))

        tools.append(FunctionTool(
            name=spec["name"],
            description=spec["description"],
            params_json_schema=spec["parameters"],
            on_invoke_tool=_invoke,
            strict_json_schema=False,
        ))
    return tools
