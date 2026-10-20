"""One long-lived bash for the run, with OpenHands terminal timeout semantics.

OpenHands 1.49.2 keeps a tmux pane (``openhands/tools/terminal``). This session
is one interactive bash on a pty, started with ``docker exec`` in the agent
container, so cwd, exports, functions, and background jobs survive across
calls. The messages and the 30s no-output timeout match
``TerminalSession`` in that package: a ``timeout`` is a hard limit and
disables the no-output pause; exit code ``-1`` means the command is still
running.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from authorization_eva_sandbox.evaluation.harness.openai_agents.workspace_tools import (
    NO_CHANGE_TIMEOUT_SECONDS,
    TIMEOUT_MESSAGE_TEMPLATE,
    clip_output,
)

_META_RE = re.compile(
    r"__AB_META__\s+(-?\d+)\s+__AB_PWD__\s+(.*?)\s+__AB_META_END__"
)
_POLL_INTERVAL = 0.1
_BOOTSTRAP = (
    "set +H; export GIT_PAGER=cat PAGER=cat PS1='' PS2=''; "
    "export PROMPT_COMMAND='printf \"\\n__AB_META__ %s __AB_PWD__ %s __AB_META_END__\\n\" \"$?\" \"$PWD\"'"
)


@dataclass
class ShellResult:
    text: str
    cwd: str
    exit_code: int
    running: bool


class PersistentBash:
    """Interactive bash. ``close`` ends the process group, including background jobs."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str,
        no_change_timeout_seconds: float = NO_CHANGE_TIMEOUT_SECONDS,
        host_cwd: str | None = None,
    ):
        self.argv = argv
        self.home = cwd
        self.cwd = cwd
        self.no_change_timeout_seconds = no_change_timeout_seconds
        self.host_cwd = host_cwd
        self.proc: subprocess.Popen[bytes] | None = None
        self._master: int | None = None
        self._buf = ""
        self._cond = threading.Condition()
        self._reader: threading.Thread | None = None
        self._running = False
        self._closed = False
        self._continue = False

    def start(self) -> None:
        if self.proc is not None:
            return
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        winsize = struct.pack("HHHH", 200, 256, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)
        self.proc = subprocess.Popen(
            self.argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.host_cwd,
            start_new_session=True,
        )
        os.close(slave)
        self._master = master
        self._reader = threading.Thread(target=self._read_loop, name="ab-bash", daemon=True)
        self._reader.start()
        self._write(_BOOTSTRAP + "\n")
        if not self._wait_for(lambda: len(self._metas()) >= 1, 8):
            raise RuntimeError("persistent bash did not print a prompt")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        master = self._master
        reader = self._reader
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
            self._master = None
        if reader is not None:
            reader.join(timeout=1)

    def execute(
        self,
        command: str,
        *,
        timeout: float | None,
        is_input: bool,
        reset: bool,
    ) -> ShellResult:
        self.start()
        command = command.strip()
        if reset and is_input:
            return self._error("Cannot use reset=True with is_input=True")
        if reset:
            home = self.home
            self.close()
            self._closed = False
            self.proc = None
            self._buf = ""
            self._running = False
            self._continue = False
            self.cwd = home
            self.start()
            text = render_completed("", 0, self.cwd)
            return ShellResult(text, self.cwd, 0, False)
        if not self._running:
            if command == "":
                return self._error("No previous running command to retrieve logs from.")
            if is_input:
                return self._error("No previous running command to interact with.")
        elif not is_input and command != "":
            body = self._since_prompt()
            return ShellResult(
                render_still_running(command, body, continued=self._continue),
                self.cwd,
                -1,
                True,
            )
        before = len(self._metas())
        if command != "":
            self._write(self._keystroke(command, is_input))
        limit = timeout if timeout is not None else None
        outcome = self._poll(before, hard_timeout=limit)
        if outcome == "done":
            metas = self._metas()
            if len(metas) <= before:
                return self._error("The terminal session ended before the command finished.")
            exit_code = int(metas[-1].group(1))
            self.cwd = metas[-1].group(2)
            body = self._output_before(metas[-1], command if not is_input else "")
            self._running = False
            self._continue = False
            return ShellResult(render_completed(body, exit_code, self.cwd), self.cwd, exit_code, False)
        continued = self._continue
        self._running = True
        self._continue = True
        body = self._since_prompt()
        if command and not is_input:
            body = _strip_echo(body, command)
        if outcome == "hard":
            text = render_hard_timeout(body, float(timeout or 0), continued=continued)
        else:
            text = render_no_change(
                body, self.no_change_timeout_seconds, continued=continued,
            )
        return ShellResult(text, self.cwd, -1, True)

    def _poll(self, before: int, *, hard_timeout: float | None) -> str:
        start = time.monotonic()
        last_change = start
        last_len = len(self._buf)
        while True:
            if len(self._metas()) > before:
                return "done"
            if self.proc is not None and self.proc.poll() is not None:
                return "done" if len(self._metas()) > before else "hard"
            now = time.monotonic()
            if len(self._buf) != last_len:
                last_len = len(self._buf)
                last_change = now
            if hard_timeout is not None and now - start >= hard_timeout:
                return "hard"
            if (
                hard_timeout is None
                and now - last_change >= self.no_change_timeout_seconds
            ):
                return "soft"
            with self._cond:
                self._cond.wait(timeout=_POLL_INTERVAL)

    def _metas(self) -> list[re.Match[str]]:
        return list(_META_RE.finditer(self._buf))

    def _since_prompt(self) -> str:
        metas = self._metas()
        start = metas[-1].end() if metas else 0
        return self._buf[start:]

    def _output_before(self, meta: re.Match[str], command: str) -> str:
        metas = self._metas()
        previous = [item for item in metas if item.end() <= meta.start()]
        start = previous[-1].end() if previous else 0
        body = self._buf[start:meta.start()]
        return _strip_echo(body, command)

    def _wait_for(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            with self._cond:
                self._cond.wait(timeout=0.05)
        return predicate()

    def _keystroke(self, command: str, is_input: bool) -> str:
        if is_input and len(command) == 3 and command.startswith("C-") and command[2].isalpha():
            return chr(ord(command[2].lower()) & 0x1F)
        return command + "\n"

    def _write(self, text: str) -> None:
        if self._master is None:
            raise RuntimeError("persistent bash is not open")
        data = text.encode()
        while data:
            written = os.write(self._master, data)
            data = data[written:]

    def _read_loop(self) -> None:
        master = self._master
        if master is None:
            return
        while not self._closed:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
            with self._cond:
                self._buf += text
                self._cond.notify_all()

    def _error(self, text: str) -> ShellResult:
        return ShellResult(render_error(text), self.cwd, -1, self._running)


def render_completed(body: str, exit_code: int, cwd: str) -> str:
    """The string OpenHands shows the model after a finished command."""
    text = _clip_body(body)
    suffix = f"\n[The command completed with exit code {exit_code}.]"
    rendered = f"{text}{suffix}\n[Current working directory: {cwd}]"
    if exit_code != -1:
        rendered += f"\n[Command finished with exit code {exit_code}]"
    return clip_output(rendered)


def render_no_change(body: str, seconds: float, *, continued: bool) -> str:
    prefix = "[Below is the output of the previous command.]\n" if continued else ""
    suffix = (
        f"\n[The command has no new output after {seconds:g} seconds. "
        f"{TIMEOUT_MESSAGE_TEMPLATE}]"
    )
    return clip_output(prefix + _clip_body(body) + suffix)


def render_hard_timeout(body: str, seconds: float, *, continued: bool) -> str:
    prefix = "[Below is the output of the previous command.]\n" if continued else ""
    suffix = (
        f"\n[The command timed out after {seconds:g} seconds. "
        f"{TIMEOUT_MESSAGE_TEMPLATE}]"
    )
    return clip_output(prefix + _clip_body(body) + suffix)


def render_still_running(command: str, body: str, *, continued: bool) -> str:
    prefix = "[Below is the output of the previous command.]\n" if continued else ""
    suffix = (
        f'\n[Your command "{command}" is NOT executed. The previous command '
        "is still running - You CANNOT send new commands until the previous "
        "command is completed. By setting `is_input` to `true`, you can "
        f"interact with the current process: {TIMEOUT_MESSAGE_TEMPLATE}]"
    )
    return render_error(prefix + _clip_body(body) + suffix)


def render_error(text: str) -> str:
    return clip_output("[An error occurred during execution.]\n" + text)


def _clip_body(body: str) -> str:
    return clip_output(body.strip("\n"))


def _strip_echo(body: str, command: str) -> str:
    text = body.lstrip("\n")
    stripped = command.lstrip()
    if stripped and text.lstrip().startswith(stripped):
        text = text.lstrip().removeprefix(stripped).lstrip("\n")
    return text.strip("\n")
