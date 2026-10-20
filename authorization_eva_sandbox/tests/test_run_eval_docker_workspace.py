"""Start one DockerWorkspace over a workspace copy. Does not call a model."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
    AllowlistedMcpGateway,
)
from authorization_eva_sandbox.evaluation.harness.tool_ir import (
    allowlist_from_actions,
)
from authorization_eva_sandbox.evaluation.run_eval import (
    AGENT_SERVER_IMAGE,
    stage_rootfs,
    HostTaskEnvironment,
    OpenHandsDockerWorkspace,
    agent_container_text,
    agent_mcp_config,
    open_task_conversation,
    sealed_system_prompt,
    stored_remote_agent,
)


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    import subprocess

    listed = subprocess.run(
        ["docker", "images", "-q", AGENT_SERVER_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    return listed.returncode == 0 and bool(listed.stdout.strip())


@unittest.skipUnless(_docker_ready(), f"{AGENT_SERVER_IMAGE} is not available")
class DockerWorkspaceCopyTests(unittest.TestCase):
    def test_container_sees_the_copy_and_leaves_the_bundle_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "tasks" / "T0005-i-g-h"
            sealed = bundle / "workspace" / "records"
            sealed.mkdir(parents=True)
            original = sealed / "transactions.csv"
            original.write_text("price,1\n")
            (bundle / "vignette.json").write_text('{"vignette_id":"V0005-i-g-h"}\n')
            (bundle / "fixtures").mkdir()
            (bundle / "fixtures" / "secret.json").write_text('{"grader":true}\n')
            runtime = OpenHandsDockerWorkspace(bundle, root / "run" / "workspace")
            try:
                workspace = runtime.start()
                listed = workspace.execute_command("ls -1 /workspace/records", cwd="/workspace")
                self.assertEqual(listed.exit_code, 0, listed.stderr)
                self.assertIn("transactions.csv", listed.stdout)
                hidden = workspace.execute_command(
                    "test ! -e /workspace/vignette.json && test ! -e /workspace/fixtures",
                    cwd="/workspace",
                )
                self.assertEqual(hidden.exit_code, 0, hidden.stdout + hidden.stderr)
                probe = workspace.execute_command(
                    "touch /workspace/container-probe && ls -1 /workspace/container-probe",
                    cwd="/workspace",
                )
                self.assertEqual(probe.exit_code, 0, probe.stderr)
            finally:
                runtime.close()
            self.assertEqual(original.read_text(), "price,1\n")
            self.assertFalse((bundle / "workspace" / "container-probe").exists())
            self.assertTrue((runtime.host_copy / "container-probe").is_file())
            self.assertFalse((runtime.host_copy / "vignette.json").exists())
            self.assertFalse((runtime.host_copy / "fixtures").exists())

    def test_rootfs_is_visible_at_its_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "tasks" / "T0004-b-c-h"
            readme = bundle / "rootfs" / "home" / "chen" / "work" / "README.md"
            readme.parent.mkdir(parents=True)
            readme.write_text("telemetry\n")
            (bundle / "workspace").mkdir(parents=True)
            runtime = OpenHandsDockerWorkspace(bundle, root / "run" / "workspace")
            try:
                workspace = runtime.start()
                stage_rootfs(workspace._container_id, bundle)
                listed = workspace.execute_command(
                    "cat /home/chen/work/README.md", cwd="/workspace",
                )
                self.assertEqual(listed.exit_code, 0, listed.stderr)
                self.assertIn("telemetry", listed.stdout)
            finally:
                runtime.close()
            self.assertEqual(readme.read_text(), "telemetry\n")

    def test_agent_container_cannot_see_the_grader_token_or_environment(self):
        import json
        bundle = (
            Path(__file__).resolve().parents[2]
            / "authorization_eva_sandbox" / "tasks" / "T0001-b-e-h"
        )
        if not (bundle / "task_config.json").is_file():
            self.skipTest("T0001 bundle is not present")
        env = HostTaskEnvironment(bundle)
        vignette = json.loads((bundle / "vignette.json").read_text())
        names, _files = allowlist_from_actions([
            step["action"] for step in vignette["assembly_blueprint"]["actions"]
        ])
        gateway = None
        runtime = None
        try:
            env.start()
            gateway = AllowlistedMcpGateway(
                env.base_url, env.openapi, names, grader_token=env.grader_token,
            )
            gateway.start()
            config = agent_mcp_config(gateway.port)
            self.assertNotIn(env.grader_token, json.dumps(config))
            self.assertNotIn(env.base_url, json.dumps(config))
            with tempfile.TemporaryDirectory() as tmp:
                runtime = OpenHandsDockerWorkspace(
                    bundle, Path(tmp) / "workspace",
                )
                workspace = runtime.start()
                visible = agent_container_text(workspace, "printenv")
                self.assertNotIn(env.grader_token, visible)
                self.assertNotIn(str(env.port), visible)
                self.assertNotIn("GRADER_TOKEN", visible)
                blocked = workspace.execute_command(
                    "curl -sS -m 2 -o /dev/null -w '%{http_code}' "
                    f"http://host.docker.internal:{env.port}/_env/audit",
                    cwd="/workspace",
                )
                self.assertEqual(blocked.exit_code, 0, blocked.stderr)
                self.assertEqual(blocked.stdout.strip(), "401")
                business = workspace.execute_command(
                    "curl -sS -m 2 -o /dev/null -w '%{http_code}' "
                    "-X POST -H 'Content-Type: application/json' "
                    "-d '{\"query\":\"eggs\"}' "
                    f"http://host.docker.internal:{env.port}"
                    "/shopping_app_instacart/search",
                    cwd="/workspace",
                )
                self.assertEqual(business.exit_code, 0, business.stderr)
                self.assertEqual(business.stdout.strip(), "401")
                reached = workspace.execute_command(
                    "curl -sS -m 3 -o /dev/null -w '%{http_code}' "
                    f"http://host.docker.internal:{gateway.port}/mcp",
                    cwd="/workspace",
                )
                self.assertEqual(reached.exit_code, 0, reached.stderr)
                self.assertNotEqual(reached.stdout.strip(), "000")
        finally:
            if runtime is not None:
                runtime.close()
            if gateway is not None:
                gateway.close()
            env.close()

    def test_server_stores_the_sealed_prompt_and_mcp_config_without_calling_a_model(self):
        import json
        bundle = (
            Path(__file__).resolve().parents[2]
            / "authorization_eva_sandbox" / "tasks" / "T0001-b-e-h"
        )
        if not (bundle / "system_prompt.txt").is_file():
            self.skipTest("T0001 bundle is not present")
        env = HostTaskEnvironment(bundle)
        vignette = json.loads((bundle / "vignette.json").read_text())
        names, _files = allowlist_from_actions([
            step["action"] for step in vignette["assembly_blueprint"]["actions"]
        ])
        gateway = None
        runtime = None
        conversation = None
        try:
            env.start()
            gateway = AllowlistedMcpGateway(
                env.base_url, env.openapi, names, grader_token=env.grader_token,
            )
            gateway.start()
            config = agent_mcp_config(gateway.port)
            with tempfile.TemporaryDirectory() as tmp:
                runtime = OpenHandsDockerWorkspace(bundle, Path(tmp) / "workspace")
                workspace = runtime.start()
                conversation = open_task_conversation(workspace, bundle, config)
                self.assertEqual(workspace.working_dir, "/workspace")
                home = workspace.execute_command("printenv HOME")
                self.assertEqual(home.exit_code, 0, home.stderr)
                self.assertEqual(home.stdout.strip(), "/workspace")
                state = workspace.execute_command(
                    "test ! -e /workspace/conversations "
                    "&& test ! -e /workspace/.openhands "
                    "&& test -d /tmp/sandbox-state/conversations"
                )
                self.assertEqual(state.exit_code, 0, state.stdout + state.stderr)
                stored = stored_remote_agent(workspace, conversation)
                encoded = json.dumps(stored)
                self.assertEqual(stored.get("system_prompt"), sealed_system_prompt(bundle))
                self.assertEqual(
                    stored["mcp_config"]["authorization-bench"]["url"],
                    config["authorization-bench"]["url"],
                )
                self.assertNotIn(env.grader_token, encoded)
                self.assertNotIn(env.base_url, encoded)
                self.assertIsNone(stored["llm"]["api_key"])
                self.assertEqual(
                    getattr(conversation.state.execution_status, "value", None),
                    "idle",
                )
        finally:
            if conversation is not None:
                conversation.close()
            if runtime is not None:
                runtime.close()
            if gateway is not None:
                gateway.close()
            env.close()


if __name__ == "__main__":
    unittest.main()
