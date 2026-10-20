"""The sealed prompt's tool line, after ``adapt_prompt``."""
from __future__ import annotations

LOCAL_TOOL_NAMES = ("terminal", "read_file", "write_file", "finish")
_MARKER = "You have these tools:"


def prompt_tool_names(prompt: str) -> list[str]:
    """Names on the line after ``You have these tools:``."""
    if _MARKER not in prompt:
        return []
    rest = prompt.split(_MARKER, 1)[1].lstrip("\n")
    line = rest.splitlines()[0] if rest else ""
    return [part.strip() for part in line.split(",") if part.strip()]


def local_tools_named(prompt: str) -> list[str]:
    """Shell, file, and finish tools the prompt lists, in that order."""
    return [name for name in prompt_tool_names(prompt) if name in LOCAL_TOOL_NAMES]


def openhands_local_tool_names(actions: list) -> list[str]:
    """Shell, file, and finish tools this harness sends, aside from MCP tools.

    Blueprint shell tools come first (``read_file``, ``write_file``,
    ``terminal``), then ``finish``. ``think`` is an OpenHands built-in the
    task prompts do not name, so it is not mounted.
    """
    from authorization_eva_sandbox.evaluation.harness.tool_ir import shell_tool_names

    return [*shell_tool_names(actions), "finish"]
