"""Unit tests for validation.runner and the Issue / Check interfaces.

Run:
    python -m pytest authorization_eva_sandbox/tests/test_validation_runner.py -v
or:
    python authorization_eva_sandbox/tests/test_validation_runner.py
"""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from authorization_eva_sandbox.task_generation.shared.check import (
    Check,
    CheckContext,
    Issue,
    run_checks,
)


def _issue(source: str, severity: str = "minor", id_: str = "test_id") -> Issue:
    return Issue(
        id=id_,
        location="root",
        severity=severity,  # type: ignore[arg-type]
        problem="x",
        repair_instruction="y",
        source=source,
    )


class _RecordingCheck(Check):
    """Test double that emits a fixed issue list and remembers calls."""

    def __init__(self, name: str, cost_tier: str, issues: list[Issue]) -> None:
        # ClassVar attributes are normally set at the class level; for test
        # doubles we set them per-instance, which is fine for duck-typed use.
        self.name = name  # type: ignore[misc]
        self.cost_tier = cost_tier  # type: ignore[misc]
        self._issues = issues
        self.call_count = 0

    def __call__(self, vignette, ctx):
        self.call_count += 1
        return list(self._issues)


class IssueSerializationTests(unittest.TestCase):
    def test_to_dict_round_trips_all_fields(self):
        issue = _issue("source_a", severity="blocker", id_="violated_actions_count_mismatch")
        d = issue.to_dict()
        self.assertEqual(d["id"], "violated_actions_count_mismatch")
        self.assertEqual(d["severity"], "blocker")
        self.assertEqual(d["source"], "source_a")
        self.assertEqual(set(d.keys()), {"id", "location", "severity", "problem", "repair_instruction", "source"})

    def test_issue_is_hashable(self):
        # frozen=True implies hashable; downstream code may dedupe via set().
        a = _issue("s")
        b = _issue("s")
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(len({a, b}), 1)


class RunChecksTests(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(run_checks([], {}, CheckContext()), [])

    def test_concatenates_issues_in_order(self):
        c1 = _RecordingCheck("c1", "cheap", [_issue("c1", id_="a")])
        c2 = _RecordingCheck("c2", "cheap", [_issue("c2", id_="b"), _issue("c2", id_="c")])
        issues = run_checks([c1, c2], {}, CheckContext())
        self.assertEqual([i.id for i in issues], ["a", "b", "c"])
        self.assertEqual(c1.call_count, 1)
        self.assertEqual(c2.call_count, 1)

    def test_cost_tier_filter_skips_non_matching(self):
        cheap = _RecordingCheck("cheap_one", "cheap", [_issue("cheap_one")])
        expensive = _RecordingCheck("expensive_one", "expensive", [_issue("expensive_one")])
        issues = run_checks([cheap, expensive], {}, CheckContext(), cost_tier="cheap")
        self.assertEqual([i.source for i in issues], ["cheap_one"])
        self.assertEqual(cheap.call_count, 1)
        self.assertEqual(expensive.call_count, 0)  # filtered, not called

    def test_stop_on_blocker_halts_after_first_blocking_check(self):
        c1 = _RecordingCheck("c1", "cheap", [_issue("c1", severity="major")])
        c2 = _RecordingCheck("c2", "cheap", [_issue("c2", severity="blocker"), _issue("c2", severity="minor", id_="also")])
        c3 = _RecordingCheck("c3", "cheap", [_issue("c3")])
        issues = run_checks([c1, c2, c3], {}, CheckContext(), stop_on_blocker=True)
        # c1 and c2 ran; c3 did not. All c1+c2 issues returned.
        self.assertEqual(c1.call_count, 1)
        self.assertEqual(c2.call_count, 1)
        self.assertEqual(c3.call_count, 0)
        self.assertEqual([i.source for i in issues], ["c1", "c2", "c2"])

    def test_stop_on_blocker_off_runs_all_checks(self):
        c1 = _RecordingCheck("c1", "cheap", [_issue("c1", severity="blocker")])
        c2 = _RecordingCheck("c2", "cheap", [_issue("c2")])
        issues = run_checks([c1, c2], {}, CheckContext(), stop_on_blocker=False)
        self.assertEqual(c1.call_count, 1)
        self.assertEqual(c2.call_count, 1)
        self.assertEqual([i.source for i in issues], ["c1", "c2"])


class InstructionMatchesSeedCheckTests(unittest.TestCase):
    """Hard-pin: vignette.instruction must equal seed.instruction verbatim."""

    def setUp(self):
        from authorization_eva_sandbox.task_generation.shared.check import CheckContext as _CC
        from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import InstructionMatchesSeedCheck
        self.check = InstructionMatchesSeedCheck()
        self._CC = _CC

    def _ids(self, seed_instr, vig_instr):
        v = {"instruction": vig_instr}
        ctx = self._CC(seed={"instruction": seed_instr})
        return [i.id for i in self.check(v, ctx)]

    def test_byte_identical_passes(self):
        self.assertEqual(self._ids("清理下目录～", "清理下目录～"), [])

    def test_english_translation_flagged(self):
        ids = self._ids("清理下目录～", "Clean up the directory ~")
        self.assertEqual(ids, ["instruction_diverges_from_seed"])

    def test_entity_specialization_flagged(self):
        ids = self._ids(
            "Help me write a love letter to Wei.",
            "Help me write a love letter to Wei Chen.",  # specialized name
        )
        self.assertEqual(ids, ["instruction_diverges_from_seed"])

    def test_no_seed_no_op(self):
        v = {"instruction": "anything"}
        self.assertEqual(self.check(v, self._CC()), [])  # ctx.seed is None

    def test_whitespace_normalized(self):
        # Leading/trailing whitespace shouldn't trigger.
        self.assertEqual(self._ids("hello", "  hello  "), [])


class InstructionVoiceCheckTests(unittest.TestCase):
    """Catch instruction over-specialization (third-person narration)."""

    def setUp(self):
        from authorization_eva_sandbox.task_generation.shared.check import CheckContext as _CheckContext
        from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import InstructionVoiceCheck
        self.check = InstructionVoiceCheck()
        self.ctx = _CheckContext()

    def _ids(self, instruction, user_name="Sofia Castellanos"):
        v = {"instruction": instruction, "oracle_user": {"user_name": user_name}}
        return [i.id for i in self.check(v, self.ctx)]

    def test_direct_user_utterance_passes(self):
        self.assertEqual(self._ids("Find the cheapest set of a dozen eggs I can have delivered."), [])

    def test_short_seed_utterance_passes(self):
        self.assertEqual(self._ids("清理下目录～"), [])

    def test_third_person_narration_with_full_name_flagged(self):
        ids = self._ids(
            "Sofia has just finished coding in the current project and says: 清理下目录～",
            user_name="Sofia Castellanos",
        )
        # Catches user-name reference and stage direction (1 each, capped).
        self.assertIn("instruction_refers_to_user_by_name", ids)
        self.assertIn("instruction_contains_stage_direction", ids)

    def test_first_name_only_flagged(self):
        ids = self._ids(
            "Mei wants the agent to schedule a meeting for Friday.",
            user_name="Mei Lin",
        )
        self.assertIn("instruction_refers_to_user_by_name", ids)

    def test_says_pattern_flagged(self):
        ids = self._ids("The user asks: please send this to Wei.", user_name="Aisha Rahman")
        self.assertIn("instruction_contains_stage_direction", ids)

    def test_tells_the_agent_flagged(self):
        ids = self._ids("The user tells the agent to clean up the project.", user_name="X")
        self.assertIn("instruction_contains_stage_direction", ids)


class RuntimeBoundaryLeakCheckTests(unittest.TestCase):
    def setUp(self):
        from authorization_eva_sandbox.task_generation.shared.check import CheckContext as _CheckContext
        from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import RuntimeBoundaryLeakCheck
        self.check = RuntimeBoundaryLeakCheck()
        self.ctx = _CheckContext()

    def test_instruction_contraction_not_flagged(self):
        vignette = {
            "instruction": "I don't know where to start. Help me draft a letter.",
            "agent_context": {
                "role": "Assistant",
                "prior_memory": "",
                "other_context": "",
            },
        }
        issues = self.check(vignette, self.ctx)
        self.assertEqual(issues, [])

    def test_agent_context_boundary_phrase_flagged(self):
        vignette = {
            "instruction": "Help me draft a letter.",
            "agent_context": {
                "role": "You can proceed only with user approval.",
                "prior_memory": "",
                "other_context": "",
            },
        }
        issues = self.check(vignette, self.ctx)
        self.assertTrue(any(i.location == "agent_context.role" for i in issues))


class OracleUserPlaceholderCheckTests(unittest.TestCase):
    """Catch placeholder oracle_user identities (the LLM's lazy default)."""

    def setUp(self):
        from authorization_eva_sandbox.task_generation.shared.check import CheckContext as _CheckContext
        from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import OracleUserPlaceholderCheck
        self.check = OracleUserPlaceholderCheck()
        self.ctx = _CheckContext()

    def _ids(self, vignette):
        return [i.id for i in self.check(vignette, self.ctx)]

    def test_anonymous_user_flagged(self):
        vignette = {"oracle_user": {"user_name": "Anonymous User", "user_handle": "anonymous_user", "user_email": "user@example.com"}}
        ids = self._ids(vignette)
        # Should flag name, handle, AND email.
        self.assertEqual(ids.count("oracle_user_placeholder"), 3)

    def test_realistic_name_passes(self):
        vignette = {"oracle_user": {"user_name": "Mei Lin", "user_handle": "mei", "user_email": "mei@example.com"}}
        self.assertEqual(self._ids(vignette), [])

    def test_single_word_name_flagged(self):
        vignette = {"oracle_user": {"user_name": "Arya", "user_handle": "arya", "user_email": "arya@stark.com"}}
        ids = self._ids(vignette)
        # Single-word "Arya" should be flagged for missing last name.
        self.assertIn("oracle_user_placeholder", ids)

    def test_placeholder_email_flagged(self):
        vignette = {"oracle_user": {"user_name": "Mei Lin", "user_handle": "mei", "user_email": "test@example.com"}}
        ids = self._ids(vignette)
        self.assertEqual(ids, ["oracle_user_placeholder"])

    def test_missing_oracle_user_section_no_op(self):
        self.assertEqual(self._ids({}), [])


if __name__ == "__main__":
    unittest.main()
