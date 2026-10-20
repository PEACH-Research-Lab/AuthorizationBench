"""Stage 4 writes the agent block from the seed trigger."""
import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from authorization_eva_sandbox.task_generation.stages.materialize.stage4 import (
    CONTEXT_OVERFLOW_DEFAULT_MAX_CHARS,
    _agent_runtime_block,
)


class TestAgentRuntimeBlock(unittest.TestCase):
    def test_context_overflow_trigger_pins_char_condenser(self):
        seed = {"situational_trigger": ["Input volume exceeded agent's context capacity"]}
        block = _agent_runtime_block(seed)
        self.assertEqual(block["condenser"],
                         {"max_chars": CONTEXT_OVERFLOW_DEFAULT_MAX_CHARS, "keep_first": 0})
        self.assertIsNone(block["max_message_chars"])
        self.assertEqual(block["max_steps"], 35)

    def test_other_triggers_leave_harness_defaults(self):
        seed = {"situational_trigger": ["The instruction contains semantic ambiguity"]}
        self.assertEqual(_agent_runtime_block(seed),
                         {"max_steps": 35, "condenser": None, "max_message_chars": None})


if __name__ == "__main__":
    unittest.main()
