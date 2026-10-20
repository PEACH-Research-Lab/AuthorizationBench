"""OpenHands 1.49.2 condenser is attached only for a token budget."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from openhands.sdk import LLM
from openhands.sdk.context.condenser.llm_summarizing_condenser import (
    LLMSummarizingCondenser,
)

from authorization_eva_sandbox.evaluation.harness.openhands_condenser import (
    SDK_VERSION,
    build_openhands_agent,
    condenser_from_task,
    require_openhands_sdk,
)


def _llm() -> LLM:
    return LLM(model="gpt-4o", api_key="test-key")


class CondenserFromTaskTests(unittest.TestCase):
    def test_requires_sdk_1_49_2(self):
        require_openhands_sdk()
        from importlib.metadata import version
        self.assertEqual(version("openhands-sdk"), SDK_VERSION)

    def test_token_budget_sets_max_tokens_and_keeps_event_default(self):
        task = {"agent": {"condenser": {"max_tokens": 3712, "keep_first": 1}}}
        condenser = condenser_from_task(task, _llm())
        self.assertIsInstance(condenser, LLMSummarizingCondenser)
        self.assertEqual(condenser.max_tokens, 3712)
        self.assertEqual(condenser.keep_first, 1)
        self.assertEqual(condenser.max_size, 240)

    def test_omitted_keep_first_defaults_to_zero(self):
        task = {"agent": {"condenser": {"max_tokens": 9000}}}
        condenser = condenser_from_task(task, _llm())
        self.assertEqual(condenser.keep_first, 0)

    def test_keep_first_zero_is_not_replaced(self):
        task = {"agent": {"condenser": {"max_tokens": 9000, "keep_first": 0}}}
        condenser = condenser_from_task(task, _llm())
        self.assertEqual(condenser.keep_first, 0)

    def test_missing_budget_leaves_condenser_off(self):
        llm = _llm()
        self.assertIsNone(condenser_from_task({"agent": {"condenser": None}}, llm))
        self.assertIsNone(condenser_from_task({"agent": {}}, llm))
        self.assertIsNone(condenser_from_task({}, llm))

    def test_character_budget_is_not_a_token_budget(self):
        task = {"agent": {"condenser": {"max_chars": 6000, "keep_first": 1}}}
        self.assertIsNone(condenser_from_task(task, _llm()))

    def test_agent_uses_the_given_prompt_and_token_budget(self):
        task = {"agent": {"condenser": {"max_tokens": 3712, "keep_first": 1}}}
        prompt = "<ROLE>\nassistant\n</ROLE>"
        agent = build_openhands_agent(task, _llm(), system_prompt=prompt, tools=[])
        self.assertEqual(agent.system_prompt, prompt)
        self.assertEqual(agent.condenser.max_tokens, 3712)
        self.assertEqual(agent.condenser.keep_first, 1)

    def test_agent_without_a_budget_has_no_condenser(self):
        task = {"agent": {"condenser": None}}
        agent = build_openhands_agent(
            task, _llm(), system_prompt="role", tools=[], condenser="ignored",
        )
        self.assertEqual(agent.system_prompt, "role")
        self.assertIsNone(agent.condenser)


if __name__ == "__main__":
    unittest.main()
