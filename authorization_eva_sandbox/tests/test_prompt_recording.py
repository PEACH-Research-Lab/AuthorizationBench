"""What each harness sends as its system text, and what the run records.

The OpenHands class needs ``.venv-openhands``; the OpenAI Agents class needs
``.venv-openai-agents``. Each skips in the other environment. No model is
called: OpenHands builds its SystemPromptEvent on ``send_message``, and the
Agents runtime runs a ScriptedModel.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.evaluation.harness import prompt_composition as pc

try:
    import agents  # noqa: F401
except ImportError:
    agents = None
try:
    import openhands.sdk  # noqa: F401
    openhands_sdk = True
except ImportError:
    openhands_sdk = False

SANDBOX = Path(__file__).resolve().parents[1]
TASKS = SANDBOX / "tasks"
VALIDATION_TASKS = (
    "T0003-g-b-h", "T0027-g-b-h", "T0015-j-c-h", "T0028-j-b-h", "T0030-h-b-n", "T0049-a-j-h",
)


def _todays_adapted_prompt(bundle: Path, model: str) -> str:
    from authorization_eva_sandbox.evaluation.harness.tool_ir import allowlist_from_actions
    from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import adapter_for_model

    vignette = json.loads((bundle / "vignette.json").read_text())
    actions = [step["action"] for step in vignette["assembly_blueprint"]["actions"]]
    names, _files = allowlist_from_actions(actions)
    return adapter_for_model(model, names).adapt_prompt((bundle / "system_prompt.txt").read_text())


@unittest.skipUnless(openhands_sdk, "openhands SDK is not installed")
class OpenHandsPromptTests(unittest.TestCase):
    def _llm(self):
        from openhands.sdk import LLM
        from pydantic import SecretStr

        return LLM(model="gpt-4o", api_key=SecretStr("not-a-model-call"))

    def test_sealed_prompt_is_sent_unchanged_and_recorded_in_full(self):
        from openhands.sdk import Conversation

        from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
            OpenHandsLocalRuntime,
            result_from_openhands_conversation,
        )
        from authorization_eva_sandbox.evaluation.harness.openhands_condenser import (
            build_openhands_agent,
        )

        model = "openrouter/anthropic/claude-sonnet-5"
        for task in VALIDATION_TASKS:
            with self.subTest(task=task):
                bundle = TASKS / task
                expected = _todays_adapted_prompt(bundle, model)
                composition = pc.compose(bundle, model, condition=pc.SEALED, harness="openhands")
                config = json.loads((bundle / "task_config.json").read_text())
                agent = build_openhands_agent(
                    config, self._llm(), system_prompt=composition.text, tools=[],
                )
                workdir = tempfile.mkdtemp(prefix="oh_prompt_")
                conversation = Conversation(agent=agent, workspace=workdir, visualizer=None)
                try:
                    conversation.send_message("start")
                    events = list(conversation.state.events)
                    system = next(e for e in events if type(e).__name__ == "SystemPromptEvent")
                    sent = [block.text for block in system.to_llm_message().content]
                    self.assertEqual(sent, [expected])
                    runtime = OpenHandsLocalRuntime(
                        conversation, "start", task=task, model=model,
                        prompt_composition=composition,
                    )
                    produced = runtime._with_prompt(result_from_openhands_conversation(
                        conversation, task=task, model=model,
                    ))
                    record = produced["metadata"]["prompt"]
                    self.assertEqual(record["condition"], "sealed")
                    self.assertEqual(record["fingerprint"], "sealed")
                    self.assertEqual(record["system"]["blocks"], [expected])
                    self.assertTrue(record["system"]["ours_verbatim"])
                    self.assertGreater(len(expected), 300)
                finally:
                    conversation.close()
                    shutil.rmtree(workdir, ignore_errors=True)

    def test_without_a_composition_nothing_is_added(self):
        from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
            OpenHandsLocalRuntime,
        )

        runtime = OpenHandsLocalRuntime(object(), "start", task="t", model="m")
        produced = {"events": [], "state": {}}
        self.assertEqual(runtime._with_prompt(dict(produced)), produced)

    def test_condenser_takes_the_effective_budget_only_when_given(self):
        from authorization_eva_sandbox.evaluation.harness.openhands_condenser import (
            build_openhands_agent,
            condenser_from_task,
        )

        config = json.loads((TASKS / "T0027-g-b-h" / "task_config.json").read_text())
        self.assertEqual(condenser_from_task(config, self._llm()).max_tokens, 7600)
        self.assertEqual(condenser_from_task(config, self._llm(), max_tokens=10904).max_tokens, 10904)
        agent = build_openhands_agent(
            config, self._llm(), system_prompt="role", tools=[], condenser_max_tokens=10904,
        )
        self.assertEqual(agent.condenser.max_tokens, 10904)
        self.assertEqual(agent.condenser.keep_first, 0)

    def test_native_sends_the_sdk_template_then_our_text_once(self):
        from openhands.sdk import Conversation

        from authorization_eva_sandbox.evaluation.harness.openhands.conversation import (
            native_agent_context,
            native_system_text,
            shell_tools_for_actions,
        )
        from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
            OpenHandsLocalRuntime,
            result_from_openhands_conversation,
        )
        from authorization_eva_sandbox.evaluation.harness.openhands_condenser import (
            build_openhands_agent,
        )

        model = "openrouter/anthropic/claude-sonnet-5"
        reference = pc.native_prompt_for("openhands").text()
        tasks = VALIDATION_TASKS + ("T0004-b-c-h", "T0034-n-g-n", "T0040-n-h-h")
        for task in tasks:
            with self.subTest(task=task):
                bundle = TASKS / task
                vignette = json.loads((bundle / "vignette.json").read_text())
                tools = shell_tools_for_actions(
                    [step["action"] for step in vignette["assembly_blueprint"]["actions"]]
                )
                composition = pc.compose(bundle, model, condition=pc.NATIVE, harness="openhands")
                config = json.loads((bundle / "task_config.json").read_text())
                native = native_system_text(self._llm(), tools)
                if {tool.name for tool in tools} == {"read_file", "write_file", "terminal"}:
                    # The reference copy is the Sonnet rendering; the SDK varies
                    # the closing section by model.
                    from openhands.sdk import LLM
                    from pydantic import SecretStr

                    sonnet = LLM(model=model, api_key=SecretStr("not-a-model-call"))
                    self.assertEqual(native_system_text(sonnet, tools), reference)
                agent = build_openhands_agent(
                    config, self._llm(), system_prompt=None, tools=tools,
                    agent_context=native_agent_context(composition.text),
                )
                workdir = tempfile.mkdtemp(prefix="oh_native_")
                conversation = Conversation(agent=agent, workspace=workdir, visualizer=None)
                try:
                    conversation.send_message("start")
                    events = list(conversation.state.events)
                    system = next(e for e in events if type(e).__name__ == "SystemPromptEvent")
                    sent = [block.text for block in system.to_llm_message().content]
                    self.assertEqual(sent, [native, composition.text.rstrip("\n")])
                    joined = "\n".join(sent)
                    self.assertEqual(joined.count(composition.sealed.rstrip("\n")), 1)
                    self.assertEqual(joined.count(pc.ENVIRONMENT_NOTES), 1)
                    self.assertEqual(
                        joined.count("<PRIOR_MEMORY>"), composition.sealed.count("<PRIOR_MEMORY>"),
                    )
                    self.assertNotIn("CURRENT_DATETIME", joined)
                    runtime = OpenHandsLocalRuntime(
                        conversation, "start", task=task, model=model,
                        prompt_composition=composition, native_text=native,
                    )
                    produced = runtime._with_prompt(result_from_openhands_conversation(
                        conversation, task=task, model=model,
                    ))
                    record = produced["metadata"]["prompt"]
                    self.assertEqual(record["condition"], "native")
                    self.assertTrue(record["fingerprint"].startswith("native-"))
                    self.assertEqual(record["system"]["blocks"], sent)
                    self.assertTrue(record["system"]["ours_verbatim"])
                    self.assertEqual(record["native"]["text_sha256"], pc.sha256_text(native))
                finally:
                    conversation.close()
                    shutil.rmtree(workdir, ignore_errors=True)

    def test_native_budget_adds_the_template_tokens_on_openhands(self):
        from authorization_eva_sandbox.evaluation.harness.condenser_budget import condenser_budget
        from authorization_eva_sandbox.evaluation.harness.openhands.conversation import (
            native_agent_context,
            native_system_text,
        )
        from authorization_eva_sandbox.evaluation.harness.openhands_condenser import (
            build_openhands_agent,
        )

        model = "openrouter/anthropic/claude-sonnet-5"
        config = json.loads((TASKS / "T0027-g-b-h" / "task_config.json").read_text())
        native = native_system_text(self._llm(), [])
        budget = condenser_budget(config, condition=pc.NATIVE, native_text=native, model=model)
        self.assertEqual(budget["task_max_tokens"], 7600)
        self.assertGreater(budget["native_prompt_tokens"], 2000)
        self.assertEqual(
            budget["effective_max_tokens"], 7600 + budget["native_prompt_tokens"],
        )
        agent = build_openhands_agent(
            config, self._llm(), system_prompt=None, tools=[],
            agent_context=native_agent_context("ours"),
            condenser_max_tokens=budget["effective_max_tokens"],
        )
        self.assertEqual(agent.condenser.max_tokens, budget["effective_max_tokens"])
        self.assertEqual(agent.condenser.keep_first, 0)
        sealed_config = json.loads((TASKS / "T0028-j-b-h" / "task_config.json").read_text())
        self.assertIsNone(
            condenser_budget(sealed_config, condition=pc.NATIVE, native_text=native, model=model)
        )


@unittest.skipUnless(agents is not None, "agents SDK is not installed")
class OpenAIAgentsPromptTests(unittest.TestCase):
    def _run(self, composition, **kwargs):
        from authorization_eva_sandbox.evaluation.harness.openai_agents.runtime import (
            OpenAIAgentsRuntime,
        )
        from authorization_eva_sandbox.evaluation.harness.openai_agents.scripted import (
            ScriptedModel,
        )

        model = ScriptedModel(["done"])
        runtime = OpenAIAgentsRuntime(
            instruction="start",
            system_prompt=composition.text,
            model=model,
            task="unit",
            model_name="gpt-4.1",
            task_timeout_seconds=30,
            max_steps=4,
            prompt_composition=composition,
            **kwargs,
        )
        return runtime.run(), model

    def test_sealed_prompt_is_sent_unchanged_and_recorded(self):
        for task in VALIDATION_TASKS:
            with self.subTest(task=task):
                bundle = TASKS / task
                expected = _todays_adapted_prompt(bundle, "gpt-4.1")
                composition = pc.compose(bundle, "gpt-4.1", condition=pc.SEALED, harness="openai_agents")
                produced, model = self._run(composition)
                self.assertEqual(model.calls[0]["system"], expected)
                record = produced["metadata"]["prompt"]
                self.assertEqual(record["condition"], "sealed")
                self.assertEqual(record["system"]["blocks"], [expected])
                self.assertEqual(produced["metadata"]["first_request"]["system"], expected)
                self.assertNotIn("condenser_budget", produced["metadata"])

    def test_native_condition_sends_our_text_alone_on_the_plain_agent(self):
        bundle = TASKS / "T0030-h-b-n"
        composition = pc.compose(bundle, "gpt-4.1", condition=pc.NATIVE, harness="openai_agents")
        produced, model = self._run(composition)
        sent = model.calls[0]["system"]
        self.assertEqual(sent, composition.text)
        self.assertTrue(sent.startswith(_todays_adapted_prompt(bundle, "gpt-4.1").rstrip("\n")))
        self.assertTrue(sent.rstrip("\n").endswith("</ENVIRONMENT_NOTES>"))
        record = produced["metadata"]["prompt"]
        self.assertEqual(record["condition"], "native")
        self.assertIsNone(record["native"])
        self.assertEqual(record["fingerprint"], pc.prompt_fingerprint(pc.NATIVE, "openai_agents"))
        self.assertTrue(record["system"]["ours_verbatim"])

    def test_native_budget_reaches_the_condenser_and_the_record(self):
        from authorization_eva_sandbox.evaluation.harness.condenser_budget import condenser_budget
        from authorization_eva_sandbox.evaluation.harness.openai_agents.bundle import _condenser

        config = json.loads((TASKS / "T0027-g-b-h" / "task_config.json").read_text())
        self.assertEqual(_condenser(config, {}, "gpt-4.1").max_tokens, 7600)
        budget = condenser_budget(
            config, condition=pc.NATIVE, native_text="x", model="gpt-4.1",
            count=lambda model, text: 3300,
        )
        self.assertEqual(_condenser(config, {}, "gpt-4.1", budget).max_tokens, 10900)
        composition = pc.compose(TASKS / "T0027-g-b-h", "gpt-4.1", condition=pc.NATIVE,
                                 harness="openai_agents")
        produced, _model = self._run(composition, condenser_budget=budget)
        self.assertEqual(produced["metadata"]["condenser_budget"], budget)


class SharedRunRecordTests(unittest.TestCase):
    def test_context_management_carries_the_native_budget(self):
        from authorization_eva_sandbox.evaluation.run_eval import context_management_record

        config = json.loads((TASKS / "T0027-g-b-h" / "task_config.json").read_text())
        plain = context_management_record(config, [])
        self.assertEqual(plain["effective_condenser"]["max_tokens"], 7600)
        self.assertNotIn("budget", plain)
        self.assertEqual(context_management_record(config, [], None), plain)
        budget = {"task_max_tokens": 7600, "native_prompt_tokens": 3300,
                  "effective_max_tokens": 10900, "model": "m", "token_counter": "x"}
        native = context_management_record(config, [], budget)
        self.assertEqual(native["effective_condenser"]["max_tokens"], 10900)
        self.assertEqual(native["budget"], budget)
        self.assertEqual(native["task_overrides"]["max_tokens"], 7600)

    def test_run_single_task_writes_prompt_budget_and_side_effect_records(self):
        from authorization_eva_sandbox.evaluation.run_eval import run_single_task

        bundle = TASKS / "T0027-g-b-h"
        composition = pc.compose(bundle, "gpt-4.1", condition=pc.NATIVE, harness="openai_agents")
        budget = {"task_max_tokens": 7600, "native_prompt_tokens": 0,
                  "effective_max_tokens": 7600, "model": "gpt-4.1", "token_counter": "x"}

        class _Runtime:
            def run(self):
                return {
                    "events": [], "native_trajectory": [],
                    "state": {"agent_state": "FINISHED", "last_error": ""},
                    "cost_summary": {},
                    "metadata": {
                        "prompt": pc.prompt_record(composition, system_blocks=[composition.text]),
                        "condenser_budget": budget,
                    },
                }

            def close(self):
                pass

        outputs = Path(tempfile.mkdtemp(prefix="prompt_run_"))
        try:
            path = run_single_task(
                base_dir=str(outputs), task_short_name=bundle.name, model="gpt-4.1",
                runtime=_Runtime(), harness="openai_agents",
                resolved_task_config=json.loads((bundle / "task_config.json").read_text()),
                outputs_path=str(outputs / "run"),
            )
            traj = json.loads((Path(path) / f"traj_{bundle.name}.json").read_text())
            meta = traj["metadata"]
            self.assertEqual(pc.recorded_condition(meta), "native")
            self.assertEqual(pc.recorded_fingerprint(meta), composition.fingerprint)
            self.assertEqual(meta["context_management"]["budget"], budget)
            self.assertEqual(meta["native_side_effects"]["flags"], {
                "ai_disclosure": False, "memory_files": False,
                "git_identity": False, "writes_outside_workspace": False,
            })
        finally:
            shutil.rmtree(outputs, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
