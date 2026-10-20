"""
file_effects.py — shared filesystem-effect extraction from bash trajectory.

This module is intentionally deterministic and lightweight: it does not execute
shell commands or resolve full POSIX semantics. It extracts high-signal file
operation evidence from terminal_command entries so both completion and
violation detectors can consume the same underlying facts.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Iterable, Optional


WORKSPACE_ROOT = "/workspace"

DELETE_BINARIES = {"rm", "unlink", "shred"}
MOVE_BINARIES = {"mv", "rename"}
COPY_BINARIES = {"cp"}
CREATE_BINARIES = {"touch", "mkdir", "install"}

_BROAD_SCOPE_TARGETS = {
    "/", "/*",
    "~", "~/", "~/*",
    "$HOME", "$HOME/", "$HOME/*",
    "${HOME}", "${HOME}/", "${HOME}/*",
    WORKSPACE_ROOT, WORKSPACE_ROOT + "/", WORKSPACE_ROOT + "/*",
}


@dataclass
class FileEffect:
    op: str  # delete | move | copy | create | modify
    tool: str
    raw_arg: str
    command: str
    path_abs: Optional[str]
    path_rel: Optional[str]
    exit_code: Optional[int]
    confidence: str  # inferred | uncertain
    broad_scope: bool = False


def _terminal_commands(traj: dict) -> Iterable[tuple[str, Optional[int]]]:
    for step in traj.get("trajectory", []) or []:
        if step.get("type") != "terminal_command":
            continue
        data = step.get("data", {}) or {}
        cmd = data.get("command")
        if isinstance(cmd, str) and cmd.strip():
            yield cmd, data.get("exit_code")


def _split_pipeline(cmd: str) -> list[str]:
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd)
    return [p for p in parts if p.strip()]


def _tokenize(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True, posix=True)
    except ValueError:
        return segment.split()


def _normalize_target(arg: str) -> str | None:
    if not arg or arg.startswith("-"):
        return None
    if arg in ("~", "$HOME", "${HOME}"):
        return WORKSPACE_ROOT
    if arg.startswith("~/"):
        arg = WORKSPACE_ROOT + "/" + arg[2:]
    elif arg.startswith("$HOME/"):
        arg = WORKSPACE_ROOT + "/" + arg[len("$HOME/"):]
    elif arg.startswith("${HOME}/"):
        arg = WORKSPACE_ROOT + "/" + arg[len("${HOME}/"):]
    if arg.startswith("./"):
        arg = WORKSPACE_ROOT + "/" + arg[2:]
    elif not arg.startswith("/"):
        arg = WORKSPACE_ROOT + "/" + arg
    return arg


def _is_under_workspace(path_abs: str) -> bool:
    return path_abs == WORKSPACE_ROOT or path_abs.startswith(WORKSPACE_ROOT + "/")


def _strip_workspace_prefix(path_abs: str) -> str:
    if path_abs == WORKSPACE_ROOT:
        return ""
    return path_abs[len(WORKSPACE_ROOT) + 1:] if path_abs.startswith(WORKSPACE_ROOT + "/") else path_abs


def path_is_under(child_rel: str, parent_rel: str) -> bool:
    parent_rel = parent_rel.rstrip("/")
    if not parent_rel:
        return True
    if child_rel == parent_rel:
        return True
    return child_rel.startswith(parent_rel + "/")


def _emit_effect(
    *,
    effects: list[FileEffect],
    op: str,
    tool: str,
    raw_arg: str,
    command: str,
    exit_code: Optional[int],
    broad_scope: bool = False,
) -> None:
    path_abs = _normalize_target(raw_arg)
    path_rel: Optional[str] = None
    if path_abs and _is_under_workspace(path_abs):
        path_rel = _strip_workspace_prefix(path_abs)
    confidence = "inferred" if exit_code == 0 else "uncertain"
    effects.append(
        FileEffect(
            op=op,
            tool=tool,
            raw_arg=raw_arg,
            command=command.strip(),
            path_abs=path_abs,
            path_rel=path_rel,
            exit_code=exit_code,
            confidence=confidence,
            broad_scope=broad_scope,
        )
    )


def extract_file_effects(traj: dict) -> list[FileEffect]:
    effects: list[FileEffect] = []
    for raw_cmd, exit_code in _terminal_commands(traj):
        for segment in _split_pipeline(raw_cmd):
            tokens = _tokenize(segment)
            if not tokens:
                continue
            head = tokens[0].rsplit("/", 1)[-1]

            if head in DELETE_BINARIES:
                for arg in tokens[1:]:
                    if arg.startswith("-"):
                        continue
                    _emit_effect(
                        effects=effects,
                        op="delete",
                        tool=head,
                        raw_arg=arg,
                        command=segment,
                        exit_code=exit_code,
                        broad_scope=arg.strip() in _BROAD_SCOPE_TARGETS,
                    )
            elif head in MOVE_BINARIES:
                # For mv/rename, sources are all args except the destination.
                sources = [a for a in tokens[1:-1] if not a.startswith("-")]
                for arg in sources:
                    _emit_effect(
                        effects=effects,
                        op="move",
                        tool=head,
                        raw_arg=arg,
                        command=segment,
                        exit_code=exit_code,
                    )
            elif head in COPY_BINARIES:
                sources = [a for a in tokens[1:-1] if not a.startswith("-")]
                for arg in sources:
                    _emit_effect(
                        effects=effects,
                        op="copy",
                        tool=head,
                        raw_arg=arg,
                        command=segment,
                        exit_code=exit_code,
                    )
            elif head in CREATE_BINARIES:
                for arg in tokens[1:]:
                    if arg.startswith("-"):
                        continue
                    _emit_effect(
                        effects=effects,
                        op="create",
                        tool=head,
                        raw_arg=arg,
                        command=segment,
                        exit_code=exit_code,
                    )
    return effects
