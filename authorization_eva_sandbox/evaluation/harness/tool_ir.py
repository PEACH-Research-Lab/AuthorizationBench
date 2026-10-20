"""Tool IR (eval-tools.json) generation.

The bundle's ``tools[]`` freezes WHICH tools exist and their descriptions;
the live environment's ``GET /openapi.json`` freezes their parameter shapes
(Pydantic request models). This module joins the two into one typed IR that
every access tier consumes:

  - builtin loop  -> to_openai_tools(ir)
  - MCP proxy     -> reads eval-tools.json (Phase 5)
  - SKILL + curl  -> render_skill_md(ir)

``message_user`` is always appended (frozen wording lives in the
user_messaging service's OpenAPI description).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import requests

MESSAGE_USER_TOOL = {
    "name": "message_user",
    "service": "user_messaging",
    "endpoint": "/user/send_message",
    "method": "POST",
}

# Infra paths never exposed as tools.
_INFRA_SUFFIXES = ("/audit", "/state", "/reset", "/health", "/inject")


def fetch_openapi(env_base_url: str) -> dict:
    resp = requests.get(f"{env_base_url}/openapi.json", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _resolve_refs(schema: Any, components: dict) -> Any:
    """Inline all $ref pointers so downstream consumers get plain JSON Schema."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return _resolve_refs(components.get(name, {}), components)
        return {k: _resolve_refs(v, components) for k, v in schema.items()
                if k not in ("title",)}
    if isinstance(schema, list):
        return [_resolve_refs(x, components) for x in schema]
    return schema


def _request_schema(op: dict, components: dict) -> dict:
    body = op.get("requestBody", {})
    schema = (body.get("content", {}).get("application/json", {}).get("schema"))
    if not schema:
        return {"type": "object", "properties": {}, "required": []}
    resolved = _resolve_refs(schema, components)
    resolved.setdefault("type", "object")
    resolved.setdefault("properties", {})
    return resolved


def build_tool_ir(task_config: dict, openapi: dict) -> list[dict]:
    """Join bundle tools[] with the live OpenAPI schema.

    Raises ValueError if a declared tool's endpoint is missing from the
    environment (referential-integrity check, mirroring the registry rule).
    """
    paths = openapi.get("paths", {})
    components = (openapi.get("components", {}) or {}).get("schemas", {}) or {}

    declared = list(task_config.get("tools", []) or [])
    if not declared:
        # Default surface: every business endpoint of every mounted service.
        declared = []
        for path, ops in sorted(paths.items()):
            if any(path.endswith(s) for s in _INFRA_SUFFIXES):
                continue
            for method, op in ops.items():
                declared.append({
                    "name": op.get("operationId") or path.strip("/").replace("/", "_"),
                    "service": path.strip("/").split("/", 1)[0],
                    "endpoint": path,
                    "method": method.upper(),
                    "description": op.get("description") or op.get("summary", ""),
                })

    if not any(t.get("name") == "message_user" for t in declared):
        declared.append(dict(MESSAGE_USER_TOOL))

    ir: list[dict] = []
    seen: set[str] = set()
    for tool in declared:
        endpoint = tool["endpoint"]
        method = tool.get("method", "POST").lower()
        op = paths.get(endpoint, {}).get(method)
        if op is None:
            raise ValueError(
                f"tool {tool.get('name')!r}: endpoint {method.upper()} {endpoint} "
                f"not found in environment OpenAPI schema")
        name = tool.get("name") or op.get("operationId")
        if name in seen:
            raise ValueError(f"duplicate tool name {name!r}")
        seen.add(name)
        ir.append({
            "name": name,
            "service": tool.get("service", endpoint.strip("/").split("/", 1)[0]),
            "endpoint": endpoint,
            "method": method.upper(),
            "description": (tool.get("description")
                            or op.get("description") or op.get("summary", "")),
            "parameters": _request_schema(op, components),
        })
    return ir


# A call that needs an id is preceded by the call on the same service that
# returns that id. The lookup is not a hint about which action to take.
_LOOKUP_BEFORE = {
    "calendar.get_event": "calendar.list_events",
    "email_client.get_message": "email_client.list_messages",
    "email_client.send_draft": "email_client.list_messages",
    "email_client.update_draft": "email_client.list_messages",
    "email_client.delete_draft": "email_client.list_messages",
    "zoom.get_transcript": "zoom.get_meeting",
    "zoom.list_participants": "zoom.get_meeting",
    "zoom.get_summary": "zoom.get_meeting",
    "zoom.create_summary_email_draft": "zoom.get_meeting",
}


def shell_tool_names(actions: list) -> list[str]:
    """File and terminal tools OpenHands mounts for one blueprint, in that order.

    ``read_file``, ``write_file``, then ``terminal``. Finish and think are
    default tools and are not included here.
    """
    flat = _flatten_actions(actions)
    names: list[str] = []
    if any(item == "runtime:shell.read_file" for item in flat):
        names.append("read_file")
    if any(item == "runtime:shell.write_file" for item in flat):
        names.append("write_file")
    if any(item == "runtime:shell.bash" for item in flat):
        names.append("terminal")
    return names


def _flatten_actions(actions: list) -> list[str]:
    flat: list[str] = []
    for action in actions:
        if isinstance(action, list):
            flat.extend(_flatten_actions(action))
        else:
            flat.append(str(action))
    return flat


def allowlist_from_actions(actions: list[str]) -> tuple[list[str], bool]:
    """MCP tool names for one blueprint, plus whether workspace files are used.

    Service actions are kept. A lookup is inserted before the action that
    needs its id. ``runtime:shell.read_file`` and ``write_file`` are file
    tools, not MCP tools. ``message_user`` is always last.
    """
    names: list[str] = []
    seen: set[str] = set()
    files = False

    def add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            names.append(name)

    for action in _flatten_actions(actions):
        if action in {"runtime:shell.read_file", "runtime:shell.write_file"}:
            files = True
            continue
        if action.startswith(("injected:", "runtime:", "builtin:")):
            continue
        if action.count(".") != 1:
            continue
        lookup = _LOOKUP_BEFORE.get(action)
        if lookup:
            add(lookup)
        add(action)
    add("message_user")
    return names, files


def render_tool_notice(names: list[str], *, files: bool, terminal: bool = False) -> str:
    """One instruction block. It names tools and does not name a port.

    ``terminal`` lists the SDK terminal tool, which is not an MCP tool.
    """
    listed = list(names)
    if terminal:
        at = listed.index("message_user") if "message_user" in listed else len(listed)
        listed.insert(at, "terminal")
    lines = [
        "You have these tools:",
        ", ".join([*listed, "finish"]),
        "An id passed to a tool must come from an earlier result of that same service.",
    ]
    if files:
        lines.append(
            "Read and write task files with read_file and write_file. "
            "Paths are under /workspace."
        )
    return "\n".join(lines)


def tool_notice_for_actions(actions: list) -> str:
    """The TOOLS block for one blueprint: exactly the tools the runner mounts.

    MCP tools come from ``allowlist_from_actions``; ``terminal``, ``read_file``,
    and ``write_file`` are the shell tools ``run_eval.shell_tools_for_actions``
    mounts for the same actions.
    """
    names, files = allowlist_from_actions(actions)
    terminal = "runtime:shell.bash" in _flatten_actions(actions)
    return render_tool_notice(names, files=files, terminal=terminal)


_TOOLS_BLOCK = re.compile(r"<TOOLS>\n.*?\n</TOOLS>", re.S)


def replace_tools_block(system_prompt: str, notice: str) -> str:
    """Put ``notice`` in the prompt's TOOLS block; add the block if it is missing."""
    block = f"<TOOLS>\n{notice}\n</TOOLS>"
    if _TOOLS_BLOCK.search(system_prompt):
        return _TOOLS_BLOCK.sub(lambda _m: block, system_prompt, count=1)
    return system_prompt.rstrip("\n") + "\n\n" + block + "\n"


def tools_block(system_prompt: str) -> str | None:
    m = _TOOLS_BLOCK.search(system_prompt)
    return m.group(0)[len("<TOOLS>\n"):-len("\n</TOOLS>")] if m else None


def public_tool_name(service: str, operation_id: str) -> str:
    """Name the agent sees. ``message_user`` stays unqualified."""
    if operation_id == "message_user":
        return "message_user"
    return f"{service}.{operation_id}"


def iter_business_tools(openapi: dict) -> list[dict]:
    """Every business operation, named ``service.operation_id``.

    Infrastructure routes and ``/_env`` are omitted, so they cannot be
    selected onto the agent surface.
    """
    paths = openapi.get("paths", {})
    components = (openapi.get("components", {}) or {}).get("schemas", {}) or {}
    tools: list[dict] = []
    seen: set[str] = set()
    for path, ops in sorted(paths.items()):
        if path.startswith("/_env") or any(path.endswith(s) for s in _INFRA_SUFFIXES):
            continue
        for method, op in ops.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            operation_id = op.get("operationId") or path.strip("/").replace("/", "_")
            service = path.strip("/").split("/", 1)[0]
            if operation_id == "message_user":
                service = "user_messaging"
            name = public_tool_name(service, operation_id)
            if name in seen:
                raise ValueError(f"duplicate business tool name {name!r}")
            seen.add(name)
            tools.append({
                "name": name,
                "service": service,
                "endpoint": path,
                "method": method.upper(),
                "description": op.get("description") or op.get("summary") or "",
                "parameters": _request_schema(op, components),
            })
    return tools


def select_allowlisted_tools(openapi: dict, names: list[str]) -> list[dict]:
    """Return exactly ``names``, in that order, from the business surface.

    A name that is missing, duplicated, or only exists as an infrastructure
    route raises. The returned list never contains those routes.
    """
    by_name = {tool["name"]: tool for tool in iter_business_tools(openapi)}
    selected: list[dict] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"duplicate allowlist name {name!r}")
        seen.add(name)
        tool = by_name.get(name)
        if tool is None:
            raise ValueError(
                f"allowlist name {name!r} is not a business tool"
            )
        selected.append(tool)
    return selected


def save_tool_ir(ir: list[dict], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(ir, indent=2))


def to_openai_tools(ir: list[dict]) -> list[dict]:
    """Chat-completions ``tools=[...]`` format (builtin loop, litellm)."""
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        },
    } for t in ir]


def render_skill_md(ir: list[dict], env_base_url: str = "http://127.0.0.1:9100") -> str:
    """SKILL.md-style API docs with curl examples (tier-3 harnesses)."""
    lines = [
        "# Environment API",
        "",
        f"All actions are HTTP calls against `{env_base_url}`. "
        "Use `curl` with a JSON body. Every endpoint returns JSON.",
        "",
    ]
    for t in ir:
        props = t["parameters"].get("properties", {})
        required = set(t["parameters"].get("required", []))
        lines.append(f"## {t['name']}")
        if t["description"]:
            lines.append(t["description"])
        example_body = {}
        if props:
            lines.append("")
            lines.append("Parameters:")
            for pname, pschema in props.items():
                ptype = pschema.get("type", "any")
                req = " (required)" if pname in required else ""
                desc = pschema.get("description", "")
                lines.append(f"- `{pname}` ({ptype}){req}: {desc}".rstrip(": "))
                example_body[pname] = pschema.get("examples", [f"<{ptype}>"])[0] \
                    if pschema.get("examples") else f"<{ptype}>"
        body = json.dumps(example_body)
        lines.append("")
        lines.append("```bash")
        lines.append(f"curl -s -X {t['method']} {env_base_url}{t['endpoint']} \\")
        lines.append("  -H 'Content-Type: application/json' \\")
        lines.append(f"  -d '{body}'")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def call_tool(ir_entry: dict, arguments: dict, env_base_url: str,
              timeout: float = 120.0) -> tuple[int, str]:
    """Execute one tool call against the environment. Returns (status, body)."""
    url = f"{env_base_url}{ir_entry['endpoint']}"
    method = ir_entry.get("method", "POST").upper()
    if method == "GET":
        resp = requests.get(url, params=arguments or None, timeout=timeout)
    else:
        resp = requests.request(method, url, json=arguments or {}, timeout=timeout)
    return resp.status_code, resp.text


def find_tool(ir: list[dict], name: str) -> Optional[dict]:
    return next((t for t in ir if t["name"] == name), None)
