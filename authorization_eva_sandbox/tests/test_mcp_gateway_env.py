"""The allowlisted MCP gateway calls the environment and hides grader material."""
from __future__ import annotations

import asyncio
import json
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fastmcp import Client

from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
    AllowlistedMcpGateway,
)
from authorization_eva_sandbox.evaluation.harness.tool_ir import (
    allowlist_from_actions,
)
from authorization_eva_sandbox.evaluation.run_eval import (
    HostTaskEnvironment,
    agent_mcp_config,
)

BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "authorization_eva_sandbox"
    / "tasks"
    / "T0001-b-e-h"
)


def _tool_names(url: str) -> list[str]:
    async def _list() -> list[str]:
        async with Client(url) as client:
            tools = await client.list_tools()
        return [tool.name for tool in tools]

    return asyncio.run(_list())


def _search_eggs(url: str):
    async def _call():
        async with Client(url) as client:
            return await client.call_tool(
                "shopping_app_instacart.search_products",
                {"query": "eggs"},
            )

    return asyncio.run(_call())


class McpGatewayEnvironmentTests(unittest.TestCase):
    def setUp(self):
        if not (BUNDLE / "task_config.json").is_file():
            self.skipTest("T0001 bundle is not present")
        self.env = HostTaskEnvironment(BUNDLE)
        vignette = json.loads((BUNDLE / "vignette.json").read_text())
        actions = [
            step["action"] for step in vignette["assembly_blueprint"]["actions"]
        ]
        self.names, _files = allowlist_from_actions(actions)
        self.env.start()
        self.gateway = AllowlistedMcpGateway(
            self.env.base_url,
            self.env.openapi,
            self.names,
            grader_token=self.env.grader_token,
        )
        self.gateway.start()

    def tearDown(self):
        gateway = getattr(self, "gateway", None)
        if gateway is not None:
            gateway.close()
        env = getattr(self, "env", None)
        if env is not None:
            env.close()

    def test_tools_list_is_the_allowlist_and_search_hits_the_environment(self):
        self.assertEqual(_tool_names(self.gateway.host_url), self.names)
        self.assertNotIn("shopping_app_instacart.state", self.names)
        self.assertNotIn("_env.audit", self.names)
        prompt = (BUNDLE / "system_prompt.txt").read_text()
        for name in self.names:
            self.assertIn(name, prompt)

        result = _search_eggs(self.gateway.host_url)
        text = result.data if isinstance(result.data, str) else json.dumps(result.data)
        self.assertIn("eggs", text.lower())
        self.assertNotIn(self.env.grader_token, text)

        config = agent_mcp_config(self.gateway.port)
        encoded = json.dumps(config)
        self.assertNotIn(self.env.grader_token, encoded)
        self.assertNotIn(self.env.base_url, encoded)
        self.assertEqual(
            config["authorization-bench"]["url"],
            f"http://host.docker.internal:{self.gateway.port}/mcp",
        )

        request = urllib.request.Request(self.env.base_url + "/_env/audit")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
