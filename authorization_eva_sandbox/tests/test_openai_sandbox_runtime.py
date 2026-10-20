"""SandboxAgent adapter: which prompt slot and which capabilities."""
from __future__ import annotations

import unittest

from agents import ModelSettings

from authorization_eva_sandbox.evaluation.harness.openai_sandbox.runtime import (
    SandboxAgentsRuntime,
)
from authorization_eva_sandbox.evaluation.harness.openhands_events import (
    record_openhands_events,
)
from authorization_eva_sandbox.trajectory_logger import TrajectoryLog


def _runtime(condition: str) -> SandboxAgentsRuntime:
    return SandboxAgentsRuntime(
        container_view=object(),
        chat_path=True,
        prompt_condition=condition,
        instruction="do the task",
        system_prompt="OUR TEXT",
        model="gpt-5.6-sol",
        task="T0000",
        model_name="openrouter/google/gemini-3.8-flash",
    )


class SandboxAgentConstructionTest(unittest.TestCase):
    def test_native_keeps_the_sdk_prompt_and_adds_ours(self) -> None:
        agent = _runtime("native")._make_agent("gpt-5.6-sol", ModelSettings(), "run_llm_again")
        self.assertEqual(agent.instructions, "OUR TEXT")
        self.assertIsNone(agent.base_instructions)
        self.assertEqual([type(cap).__name__ for cap in agent.capabilities], ["Shell", "Filesystem"])

    def test_sealed_replaces_the_sdk_prompt(self) -> None:
        agent = _runtime("sealed")._make_agent("gpt-5.6-sol", ModelSettings(), "run_llm_again")
        self.assertIsNone(agent.instructions)
        self.assertEqual(agent.base_instructions, "OUR TEXT")

    def test_responses_path_keeps_the_custom_apply_patch(self) -> None:
        runtime = _runtime("native")
        runtime.chat_path = False
        agent = runtime._make_agent("gpt-5.6-sol", ModelSettings(), "run_llm_again")
        self.assertIsNone(agent.capabilities[1].configure_tools)

    def test_chat_path_replaces_apply_patch_when_tools_bind(self) -> None:
        agent = _runtime("native")._make_agent("gpt-5.6-sol", ModelSettings(), "run_llm_again")
        self.assertIsNotNone(agent.capabilities[1].configure_tools)

    def test_run_config_uses_the_attached_session(self) -> None:
        runtime = _runtime("native")
        runtime.session = object()
        config = runtime._run_config()
        self.assertIs(config.sandbox.session, runtime.session)
        self.assertIsNone(config.call_model_input_filter)

    def test_custom_apply_patch_and_exec_command_become_judge_steps(self) -> None:
        runtime = _runtime("native")
        runtime._ensure_input([
            {"type": "function_call", "call_id": "s1", "name": "exec_command",
             "arguments": {"cmd": "echo hi"}},
            {"type": "function_call_output", "call_id": "s1",
             "output": "Process exited with code 0\nOutput:\nhi"},
            {"type": "custom_tool_call", "call_id": "p1", "name": "apply_patch",
             "input": "*** Begin Patch\n*** Add File: note.txt\n+hi\n*** End Patch"},
            {"type": "custom_tool_call_output", "call_id": "p1", "output": "applied"},
        ])
        log = TrajectoryLog(task_id="t", model="m")
        record_openhands_events(log, runtime.events)
        kinds = [step.type for step in log.steps]
        self.assertEqual(kinds, ["terminal_command", "file_edit"])
        self.assertEqual(log.steps[0].data["native_tool"], "exec_command")
        self.assertEqual(log.steps[1].data["native_tool"], "apply_patch")
        self.assertEqual(log.steps[1].data["command"], "create")

    def test_metadata_names_the_harness_and_the_patch_form(self) -> None:
        runtime = _runtime("native")
        meta = runtime._metadata()
        self.assertEqual(meta["harness"], "openai_sandbox")
        self.assertEqual(meta["apply_patch_form"], "function")
        self.assertEqual(meta["memories_at_start"], [])
