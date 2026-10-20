from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from authorization_eva_sandbox.task_generation.shared.artifact_gate import (
    attach_gate_receipt,
    gate_receipt_matches,
    make_gate_receipt,
    stored_gate_passed,
)
from authorization_eva_sandbox.task_generation.run_v3 import parse_args


class ArtifactGateReceiptTests(unittest.TestCase):
    def test_receipt_is_stable_for_equivalent_json_inputs(self):
        left = make_gate_receipt(
            stage="2b",
            passed=True,
            inputs={"upstream": {"b": 2, "a": 1}},
            models={"critic": "gpt5_6_sol", "generator": "opus_5"},
        )
        right = make_gate_receipt(
            stage="2b",
            passed=True,
            inputs={"upstream": {"a": 1, "b": 2}},
            models={"generator": "opus_5", "critic": "gpt5_6_sol"},
        )
        self.assertEqual(left, right)

    def test_changed_input_invalidates_cached_pass(self):
        artifact: dict = {"step2_script": {}}
        attach_gate_receipt(
            artifact,
            stage="2b",
            passed=True,
            inputs={"story": {"narrative": "first"}},
            models={"generator": "opus_5", "critic": "gpt5_6_sol"},
        )
        self.assertTrue(gate_receipt_matches(
            artifact,
            stage="2b",
            inputs={"story": {"narrative": "first"}},
            models={"generator": "opus_5", "critic": "gpt5_6_sol"},
        ))
        self.assertFalse(gate_receipt_matches(
            artifact,
            stage="2b",
            inputs={"story": {"narrative": "changed"}},
            models={"generator": "opus_5", "critic": "gpt5_6_sol"},
        ))

    def test_changed_model_role_invalidates_cached_pass(self):
        artifact: dict = {}
        attach_gate_receipt(
            artifact,
            stage="3",
            passed=True,
            inputs={"script": {}},
            models={"generator": "gpt5_6_sol", "critic": "opus_5"},
        )
        changed = copy.deepcopy(artifact)
        changed["_gate"]["models"]["critic"] = "gpt5_6_sol"
        self.assertFalse(gate_receipt_matches(
            changed,
            stage="3",
            inputs={"script": {}},
            models={"generator": "gpt5_6_sol", "critic": "opus_5"},
        ))

    def test_only_current_receipt_counts_as_pass(self):
        artifact: dict = {}
        attach_gate_receipt(
            artifact,
            stage="4",
            passed=True,
            inputs={"blueprint": {}},
        )
        self.assertTrue(stored_gate_passed(artifact, stage="4"))
        self.assertFalse(stored_gate_passed(artifact, stage="5"))
        artifact["_gate"]["passed"] = False
        self.assertFalse(stored_gate_passed(artifact, stage="4"))


class GenerationModelRoleTests(unittest.TestCase):
    def test_frozen_default_model_roles(self):
        with patch("sys.argv", ["run_v3", "--seed-id", "S0010"]):
            args = parse_args()
        self.assertEqual(args.stage1_profile, "glm_5_3")
        self.assertEqual(args.stage1_critic_profile, "gpt5_6_sol")
        self.assertEqual(args.stage2_profile, "gpt5_6_sol")
        self.assertEqual(args.stage2_critic_profile, "opus_4_8")
        self.assertEqual(args.stage3_profile, "gpt5_6_sol")
        self.assertEqual(args.stage3_critic_profile, "opus_4_8")
        self.assertEqual(args.stage4_profile, "opus_4_8")
        self.assertEqual(args.stage4_critic_profile, "gpt5_6_sol")
        self.assertEqual(args.actor_profile, "gpt4_1_mini")
        self.assertEqual(args.oracle_profile, "mimo_v2_6_pro_oracle")


if __name__ == "__main__":
    unittest.main()
