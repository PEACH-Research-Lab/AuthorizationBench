"""Prompt conditions: the sealed text is unchanged, the native text adds the notes."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from authorization_eva_sandbox.evaluation.harness import prompt_composition as pc
from authorization_eva_sandbox.evaluation.harness.condenser_budget import (
    condenser_budget,
    native_prompt_tokens,
    task_max_tokens,
)

SANDBOX = Path(__file__).resolve().parents[1]
TASKS = sorted(path for path in (SANDBOX / "tasks").glob("T*") if path.is_dir())
PLAN = SANDBOX / "docs" / "v4" / "HARNESS-OPENAI-AGENTS-PLAN.md"
BENCHMARK_MODELS = (
    "gpt-5.6-sol",
    "openrouter/anthropic/claude-sonnet-5",
    "openrouter/google/gemini-3.8-flash",
    "openrouter/z-ai/glm-5.3",
)
CONDENSER_TASKS = {"T0003-g-b-h", "T0003-g-b-n", "T0027-g-b-h", "T0027-g-b-n"}


def _todays_adapted_prompt(bundle: Path, model: str) -> str:
    """What openhands/bundle.py and openai_agents/bundle.py sent at a9483f1f."""
    from authorization_eva_sandbox.evaluation.harness.tool_ir import allowlist_from_actions
    from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import adapter_for_model

    vignette = json.loads((bundle / "vignette.json").read_text())
    actions = [step["action"] for step in vignette["assembly_blueprint"]["actions"]]
    names, _files = allowlist_from_actions(actions)
    prompt = (bundle / "system_prompt.txt").read_text()
    if not prompt.strip():
        raise ValueError("empty sealed prompt")
    adapter = adapter_for_model(model, names)
    return adapter.adapt_prompt(prompt) if adapter is not None else prompt


class ComposerTests(unittest.TestCase):
    def test_all_190_tasks_are_checked(self):
        self.assertEqual(len(TASKS), 190)

    def test_sealed_text_equals_todays_adapted_prompt_for_every_task(self):
        for bundle in TASKS:
            for model in BENCHMARK_MODELS:
                expected = _todays_adapted_prompt(bundle, model)
                for harness in pc.NATIVE_PROMPTS:
                    with self.subTest(task=bundle.name, model=model, harness=harness):
                        composed = pc.compose(bundle, model, condition=pc.SEALED, harness=harness)
                        self.assertEqual(composed.text, expected)
                        self.assertEqual(composed.sealed, expected)
                        self.assertIsNone(composed.native)

    def test_native_text_is_the_whole_sealed_prompt_then_the_notes(self):
        for bundle in TASKS:
            with self.subTest(task=bundle.name):
                sealed = _todays_adapted_prompt(bundle, "gpt-5.6-sol")
                composed = pc.compose(bundle, "gpt-5.6-sol", condition=pc.NATIVE, harness="openhands")
                self.assertEqual(
                    composed.text, sealed.rstrip("\n") + "\n\n" + pc.ENVIRONMENT_NOTES + "\n",
                )
                self.assertEqual(composed.text.count("<ENVIRONMENT_NOTES>"), 1)
                self.assertEqual(
                    composed.text.count("<PRIOR_MEMORY>"), sealed.count("<PRIOR_MEMORY>"),
                )
                self.assertLessEqual(composed.text.count("<PRIOR_MEMORY>"), 1)

    def test_environment_notes_are_the_approved_text(self):
        plan = PLAN.read_text()
        block = re.search(
            r"^`<ENVIRONMENT_NOTES>` \(approved [^\n]*\):\n\n```\n(.*?)\n```", plan, re.S | re.M,
        ).group(1)
        self.assertEqual(pc.ENVIRONMENT_NOTES, block)

    def test_native_prompts_are_the_reference_copies(self):
        for harness, native in pc.NATIVE_PROMPTS.items():
            with self.subTest(harness=harness):
                if native is None:
                    self.assertEqual(harness, "openai_agents")
                    continue
                self.assertTrue(native.path.is_file(), native.path)
                self.assertEqual(native.sha256(), pc.sha256_text(native.path.read_text()))
                self.assertEqual(native.path.parent, SANDBOX / "docs" / "v4" / "harness-prompts")

    def test_unknown_condition_or_harness_is_refused(self):
        with self.assertRaises(ValueError):
            pc.compose(TASKS[0], "gpt-5.6-sol", condition="both", harness="openhands")
        with self.assertRaises(ValueError):
            pc.prompt_fingerprint(pc.NATIVE, "no_such_harness")


class FingerprintTests(unittest.TestCase):
    def test_sealed_runs_and_runs_without_a_record_share_one_fingerprint(self):
        for harness in pc.NATIVE_PROMPTS:
            self.assertEqual(pc.prompt_fingerprint(pc.SEALED, harness), "sealed")
        self.assertEqual(pc.recorded_fingerprint({}), "sealed")
        self.assertEqual(pc.recorded_fingerprint({"prompt": None}), "sealed")
        self.assertEqual(pc.recorded_condition({"bundle": {}}), "sealed")

    def test_native_fingerprint_follows_harness_prompt_notes_and_composer(self):
        prints = {h: pc.prompt_fingerprint(pc.NATIVE, h) for h in pc.NATIVE_PROMPTS}
        self.assertEqual(len(set(prints.values())), len(prints))
        self.assertTrue(all(value.startswith("native-") for value in prints.values()))
        with mock.patch.object(pc, "ENVIRONMENT_NOTES", pc.ENVIRONMENT_NOTES + " "):
            self.assertNotEqual(pc.prompt_fingerprint(pc.NATIVE, "openhands"), prints["openhands"])
        with mock.patch.object(pc, "COMPOSER_VERSION", "other"):
            self.assertNotEqual(pc.prompt_fingerprint(pc.NATIVE, "openhands"), prints["openhands"])
        other = pc.NativePrompt("openhands", "x", "1.49.2", "openai-agents-0.20.0-sandbox-agent.txt")
        with mock.patch.dict(pc.NATIVE_PROMPTS, {"openhands": other}):
            self.assertNotEqual(pc.prompt_fingerprint(pc.NATIVE, "openhands"), prints["openhands"])


class PromptRecordTests(unittest.TestCase):
    def test_sealed_record_keeps_the_sent_text_and_the_condition(self):
        bundle = SANDBOX / "tasks" / "T0010-l-b-h"
        composed = pc.compose(bundle, "gpt-5.6-sol", condition=pc.SEALED, harness="openhands")
        record = pc.prompt_record(composed, system_blocks=[composed.text])
        self.assertEqual(record["condition"], "sealed")
        self.assertEqual(record["fingerprint"], "sealed")
        self.assertEqual(record["composer_version"], pc.COMPOSER_VERSION)
        self.assertIsNone(record["native"])
        self.assertNotIn("environment_notes_sha256", record)
        self.assertEqual(record["system"]["blocks"], [composed.text])
        self.assertTrue(record["system"]["ours_verbatim"])
        self.assertEqual(record["ours"]["sha256"], pc.sha256_text(composed.text))
        json.dumps(record)

    def test_native_record_names_the_native_prompt_and_its_hash(self):
        bundle = SANDBOX / "tasks" / "T0010-l-b-h"
        composed = pc.compose(bundle, "gpt-5.6-sol", condition=pc.NATIVE, harness="openai_sandbox")
        native_text = pc.NATIVE_PROMPTS["openai_sandbox"].text()
        record = pc.prompt_record(
            composed,
            system_blocks=[native_text + "\n\n" + composed.text],
            native_text=native_text,
        )
        self.assertEqual(record["condition"], "native")
        self.assertEqual(record["fingerprint"], pc.prompt_fingerprint(pc.NATIVE, "openai_sandbox"))
        self.assertEqual(record["native"]["name"], "openai-agents-sandbox-agent")
        self.assertEqual(record["native"]["version"], "0.20.0")
        self.assertEqual(record["native"]["sha256"], pc.sha256_text(native_text))
        self.assertEqual(record["native"]["text_sha256"], pc.sha256_text(native_text))
        self.assertEqual(record["environment_notes_sha256"], pc.sha256_text(pc.ENVIRONMENT_NOTES))
        self.assertTrue(record["system"]["ours_verbatim"])
        broken = pc.prompt_record(composed, system_blocks=[native_text])
        self.assertFalse(broken["system"]["ours_verbatim"])

    def test_plain_agent_native_record_has_no_native_prompt(self):
        bundle = SANDBOX / "tasks" / "T0010-l-b-h"
        composed = pc.compose(bundle, "gpt-5.6-sol", condition=pc.NATIVE, harness="openai_agents")
        record = pc.prompt_record(composed, system_blocks=[composed.text])
        self.assertIsNone(record["native"])
        self.assertEqual(record["system"]["blocks"], [composed.text])


class CondenserBudgetTests(unittest.TestCase):
    def test_only_the_four_trigger_g_tasks_have_a_token_budget(self):
        found = set()
        for bundle in TASKS:
            config = json.loads((bundle / "task_config.json").read_text())
            if task_max_tokens(config) is not None:
                found.add(bundle.name)
        self.assertEqual(found, CONDENSER_TASKS)

    def test_native_budget_adds_the_native_prompt_tokens(self):
        config = json.loads((SANDBOX / "tasks" / "T0027-g-b-h" / "task_config.json").read_text())
        task = task_max_tokens(config)
        calls = []

        def count(model: str, text: str) -> int:
            calls.append((model, text))
            return 3700

        budget = condenser_budget(
            config, condition=pc.NATIVE, native_text="native prompt", model="m", count=count,
        )
        self.assertEqual(budget["task_max_tokens"], task)
        self.assertEqual(budget["native_prompt_tokens"], 3700)
        self.assertEqual(budget["effective_max_tokens"], task + 3700)
        self.assertEqual(budget["model"], "m")
        self.assertEqual(calls, [("m", "native prompt")])

    def test_sealed_condition_and_tasks_without_a_budget_are_unchanged(self):
        config = json.loads((SANDBOX / "tasks" / "T0027-g-b-h" / "task_config.json").read_text())
        self.assertIsNone(condenser_budget(config, condition=pc.SEALED, native_text="x", model="m"))
        plain = json.loads((SANDBOX / "tasks" / "T0010-l-b-h" / "task_config.json").read_text())
        self.assertIsNone(condenser_budget(plain, condition=pc.NATIVE, native_text="x", model="m"))

    def test_no_native_prompt_adds_nothing(self):
        config = {"agent": {"condenser": {"max_tokens": 8000, "keep_first": 0}}}
        budget = condenser_budget(config, condition=pc.NATIVE, native_text="", model="m")
        self.assertEqual(budget["native_prompt_tokens"], 0)
        self.assertEqual(budget["effective_max_tokens"], 8000)

    def test_litellm_counts_the_native_prompts(self):
        try:
            import litellm
        except ImportError:
            self.skipTest("litellm is not installed in this environment")
        text = pc.NATIVE_PROMPTS["openhands"].text()
        for model in BENCHMARK_MODELS:
            with self.subTest(model=model):
                tokens = native_prompt_tokens(text, model)
                self.assertEqual(tokens, int(litellm.token_counter(model=model, text=text)))
                self.assertGreater(tokens, 2000)
                self.assertLess(tokens, 8000)


if __name__ == "__main__":
    unittest.main()
