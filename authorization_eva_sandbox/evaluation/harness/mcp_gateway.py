"""MCP gateway for one task allowlist.

The evaluator holds the environment base URL. Each listed tool is a business
call against that API. Infrastructure routes are not registered. The gateway
listens on the host so an agent container can reach it; the grader token and
the environment URL are not part of that listener.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any

import requests
import uvicorn

from .tool_ir import select_allowlisted_tools
from .tool_name_adapter import ToolNameAdapter


def proxy_tool_call(
    env_base_url: str,
    entry: dict,
    arguments: dict,
    *,
    grader_token: str | None = None,
) -> dict:
    """Call one allowlisted endpoint. The URL is not part of the tool schema.

    When the environment requires the grader token on business routes, the
    gateway sends it. The token is not copied into the tool schema or result.
    """
    url = f"{env_base_url.rstrip('/')}{entry['endpoint']}"
    method = entry.get("method", "POST").upper()
    headers = {"Authorization": f"Bearer {grader_token}"} if grader_token else None
    if method == "GET":
        resp = requests.get(url, params=arguments or None, headers=headers, timeout=30)
    else:
        resp = requests.request(
            method, url, json=arguments or {}, headers=headers, timeout=30,
        )
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    return {"status": resp.status_code, "body": body}


def build_mcp_server(
    env_base_url: str,
    openapi: dict,
    names: list[str],
    *,
    tool_names: ToolNameAdapter | None = None,
    grader_token: str | None = None,
):
    """A FastMCP server whose tools/list is exactly ``names``.

    ``names`` are canonical. When ``tool_names`` is set, the model sees the
    published identifier names; each call still reaches the canonical endpoint.
    """
    from fastmcp import FastMCP
    from fastmcp.tools.function_tool import FunctionTool

    selected = select_allowlisted_tools(openapi, names)
    server = FastMCP("authorization-bench")
    for entry in selected:
        def _call(
            _entry: dict = entry,
            _token: str | None = grader_token,
            **arguments: Any,
        ) -> dict:
            return proxy_tool_call(
                env_base_url, _entry, arguments, grader_token=_token,
            )

        published = entry["name"] if tool_names is None else tool_names.publish(entry["name"])
        server.add_tool(FunctionTool(
            name=published,
            description=entry["description"] or entry["name"],
            parameters=entry["parameters"],
            fn=_call,
        ))
    return server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AllowlistedMcpGateway:
    """Host MCP server whose tools/list is one task allowlist.

    The process listens on all interfaces so the agent container can use
    ``host.docker.internal``. It does not receive the grader token.
    """

    def __init__(
        self,
        env_base_url: str,
        openapi: dict,
        names: list[str],
        *,
        grader_token: str | None = None,
        tool_names: ToolNameAdapter | None = None,
    ):
        self.env_base_url = env_base_url
        self.names = list(names)
        self.port = _free_port()
        self._mcp = build_mcp_server(
            env_base_url,
            openapi,
            self.names,
            grader_token=grader_token,
            tool_names=tool_names,
        )
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def host_url(self) -> str:
        """URL for a client on the evaluator host."""
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> str:
        app = self._mcp.http_app(path="/mcp", transport="http")
        config = uvicorn.Config(
            app, host="0.0.0.0", port=self.port, log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._server.started:
                return self.host_url
            if not self._thread.is_alive():
                raise RuntimeError("MCP gateway exited before it started")
            time.sleep(0.05)
        raise TimeoutError(f"MCP gateway did not start on {self.host_url}")

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
