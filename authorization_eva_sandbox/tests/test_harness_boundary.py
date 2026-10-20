"""Step 0 harness boundary: run numbers, report paths, and run_eval re-exports."""
from __future__ import annotations

import importlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from authorization_eva_sandbox.evaluation.harness.agent_container import (
    agent_container_spec,
    docker_workspace_kwargs,
    sandbox_process_env,
)
from authorization_eva_sandbox.evaluation.harness.layout import report_dir_for_trajectory
from authorization_eva_sandbox.evaluation.run_eval import (
    HostNativeTrajectory,
    _resolve_outputs_path,
    main,
)


class AtomicRunNumberTests(unittest.TestCase):
    def test_two_threads_claim_distinct_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            barrier = threading.Barrier(2)
            found: list[str] = []
            errors: list[BaseException] = []

            def claim() -> None:
                try:
                    barrier.wait()
                    found.append(_resolve_outputs_path(
                        tmp, "T0004-b-c-h", "openai/gpt-4.1",
                    ))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(set(found)), 2)
            self.assertTrue(all(os.path.isdir(path) for path in found))
            for path in found:
                native = HostNativeTrajectory(path, "T0004-b-c-h", None)
                self.assertTrue(native.path.is_file())

    def test_other_harness_does_not_share_the_openhands_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            openhands = _resolve_outputs_path(tmp, "T0004-b-c-h", "gpt-4.1")
            other = _resolve_outputs_path(
                tmp, "T0004-b-c-h", "gpt-4.1", harness="openai_agents",
            )
            self.assertTrue(openhands.endswith(
                "gpt-4.1/output_full_T0004-b-c-h_gpt-4.1_1"
            ))
            self.assertFalse("/openai_agents/" in openhands)
            self.assertTrue(other.endswith(
                "openai_agents/gpt-4.1/output_full_T0004-b-c-h_gpt-4.1_openai_agents_1"
            ))


class ReportPathTests(unittest.TestCase):
    def test_openai_agents_report_does_not_collide_with_openhands(self):
        root = Path("/virtual/post_hoc_eval_report")
        openhands = Path(
            "/virtual/outputs/gpt-4.1/output_full_T0004-b-c-h_gpt-4.1_1"
            "/traj_T0004-b-c-h.json"
        )
        other = Path(
            "/virtual/outputs/openai_agents/gpt-4.1"
            "/output_full_T0004-b-c-h_gpt-4.1_openai_agents_1/traj_T0004-b-c-h.json"
        )
        openhands_dir = report_dir_for_trajectory(
            root, openhands, setting_fallback="gpt-4.1",
        )
        other_dir = report_dir_for_trajectory(
            root, other, setting_fallback="gpt-4.1",
        )
        self.assertEqual(openhands_dir, root / "openhands-sealed" / "gpt-4.1")
        self.assertEqual(other_dir, root / "openai_agents" / "gpt-4.1")
        openhands_report = openhands_dir / f"{openhands.parent.name}.md"
        other_report = other_dir / f"{other.parent.name}.md"
        self.assertNotEqual(openhands_report, other_report)


class RunEvalReexportTests(unittest.TestCase):
    def test_moved_names_are_the_same_objects(self):
        import authorization_eva_sandbox.evaluation.run_eval as run_eval

        pairs = {
            "OpenHandsDockerWorkspace": "authorization_eva_sandbox.evaluation.harness.openhands.workspace",
            "OpenHandsLocalRuntime": "authorization_eva_sandbox.evaluation.harness.openhands.runtime",
            "open_task_conversation": "authorization_eva_sandbox.evaluation.harness.openhands.conversation",
            "shell_tools_for_actions": "authorization_eva_sandbox.evaluation.harness.openhands.conversation",
            "stored_remote_agent": "authorization_eva_sandbox.evaluation.harness.openhands.conversation",
            "agent_container_text": "authorization_eva_sandbox.evaluation.harness.openhands.conversation",
            "PLACEHOLDER_MODEL_KEY": "authorization_eva_sandbox.evaluation.harness.openhands.conversation",
            "llm_from_profile": "authorization_eva_sandbox.evaluation.harness.openhands.llm",
            "effort_from_profile": "authorization_eva_sandbox.evaluation.harness.openhands.llm",
            "run_openhands_bundle": "authorization_eva_sandbox.evaluation.harness.openhands.bundle",
            "pending_agent_reply": "authorization_eva_sandbox.evaluation.harness.openhands.runtime",
            "cost_summary_from_metrics": "authorization_eva_sandbox.evaluation.harness.openhands.runtime",
            "result_from_openhands_conversation": "authorization_eva_sandbox.evaluation.harness.openhands.runtime",
            "DEFAULT_TASK_TIMEOUT_SECONDS": "authorization_eva_sandbox.evaluation.harness.openhands.runtime",
            "build_openhands_agent": "authorization_eva_sandbox.evaluation.harness.openhands_condenser",
            "AGENT_SERVER_IMAGE": "authorization_eva_sandbox.evaluation.harness.agent_container",
            "CONTAINER_WORKSPACE": "authorization_eva_sandbox.evaluation.harness.agent_container",
            "SANDBOX_STATE_DIR": "authorization_eva_sandbox.evaluation.harness.agent_container",
            "sandbox_process_env": "authorization_eva_sandbox.evaluation.harness.agent_container",
            "docker_workspace_volume": "authorization_eva_sandbox.evaluation.harness.agent_container",
            "prepare_agent_workspace_copy": "authorization_eva_sandbox.evaluation.harness.agent_container",
            "agent_home": "authorization_eva_sandbox.evaluation.harness.agent_container",
            "_resolve_outputs_path": "authorization_eva_sandbox.evaluation.harness.layout",
            "_forwarded_container_env": "authorization_eva_sandbox.evaluation.harness.openhands.workspace",
        }
        for name, module_name in pairs.items():
            module = importlib.import_module(module_name)
            self.assertIs(getattr(run_eval, name), getattr(module, name), name)

    def test_importing_run_eval_does_not_import_the_openhands_sdk(self):
        script = (
            "import sys\n"
            "import authorization_eva_sandbox.evaluation.run_eval as run_eval\n"
            "assert 'openhands' not in sys.modules\n"
            "assert run_eval.OpenHandsLocalRuntime.__module__.endswith('runtime')\n"
        )
        env = dict(os.environ)
        env["OPENHANDS_SUPPRESS_BANNER"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_unknown_harness_is_not_implemented(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["--task", "not-a-task", "--harness", "openclaw"])
        self.assertEqual(code, 2)
        self.assertIn("not implemented yet", stderr.getvalue())


class AgentContainerTests(unittest.TestCase):
    def test_docker_workspace_kwargs_match_the_previous_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp) / "copy"
            host.mkdir()
            home = "/workspace"
            spec = agent_container_spec(host, home=home, platform="linux/arm64")
            env = sandbox_process_env(home)
            expected = {
                "server_image": "authorizationbench-openhands:1.49.2",
                "working_dir": home,
                "platform": "linux/arm64",
                "volumes": [f"{host.resolve()}:/workspace"],
                "forward_env": [
                    "DEBUG", "SESSION_API_KEY", "OH_SESSION_API_KEYS_0", *env,
                ],
                "detach_logs": False,
            }
            self.assertEqual(docker_workspace_kwargs(spec), expected)
            self.assertIsNone(spec.user)
            self.assertEqual(spec.image_user, "openhands")
            self.assertIsNone(spec.network_mode)
            self.assertEqual(spec.extra_hosts, ())
            self.assertEqual(spec.labels, ())
            self.assertEqual(spec.host_reachability, "host.docker.internal")
            self.assertEqual(spec.env_dict(), env)


if __name__ == "__main__":
    unittest.main()
