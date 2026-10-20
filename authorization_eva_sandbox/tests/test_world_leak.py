from __future__ import annotations

import unittest

from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    build_fill_envelope,
)
from authorization_eva_sandbox.task_generation.shared.world_leak import (
    build_leak_units,
    scan_world_leaks,
)
from authorization_eva_sandbox.task_generation.shared.world_realization import (
    compile_world_realization,
)
from authorization_eva_sandbox.tests.test_world_fill_envelope import _load
from authorization_eva_sandbox.tests.test_world_realization_contract import (
    joint_graph_to_realization,
)


def _finding(where: str, problem: str, suggestion: str) -> str:
    return (
        "FINDING\n"
        f"WHERE: {where}\n"
        f"PROBLEM: {problem}\n"
        f"SUGGESTION: {suggestion}\n"
        "END"
    )


class WorldLeakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()
        cls.envelope = build_fill_envelope(
            scenario_id="V0003-g-b-h",
            policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
            script=_load("V0003-g-b-h", "script"),
            blueprint=_load("V0003-g-b-h", "blueprint"),
            registry=cls.registry,
        )
        cls.realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), cls.envelope
        )
        (
            cls.fixtures,
            cls.runtime,
            cls.events,
            cls.reports,
            issues,
        ) = compile_world_realization(
            cls.realization,
            envelope=cls.envelope,
            registry=cls.registry,
            reference_time="2026-06-06T10:11:00",
            principal={
                "user_name": "Priya Sharma",
                "user_email": "priya@example.com",
            },
        )
        if issues:
            raise AssertionError(f"V0003 compile failed: {issues}")
        cls.violated = (
            _load("V0003-g-b-h", "policy_skeleton")
            ["authorization_scope"]["violated_actions"]
        )
        cls.units = build_leak_units(
            envelope=cls.envelope,
            realization=cls.realization,
            registry=cls.registry,
            fixtures=cls.fixtures,
            runtime=cls.runtime,
            events=cls.events,
        )
        cls.body = next(
            unit for unit in cls.units
            if unit.location.kind == "field"
            and str(unit.location.address.get("field_path")) == "emails[].body"
        )
        cls.collection = next(
            unit for unit in cls.units if unit.location.kind == "collection"
        )

    def _scan(self, critic, cleanup=None, oracle_facts=None):
        return scan_world_leaks(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            violated_actions=self.violated,
            critic_call=critic,
            cleanup_call=cleanup,
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            oracle_facts=oracle_facts,
        )

    def test_pass_when_no_boundary_coaching(self):
        prompts: list[str] = []

        def _call(prompt: str) -> str:
            prompts.append(prompt)
            self.assertIn("NOT leaks", prompt)
            self.assertIn("merely exposing that an action exists", prompt)
            return "PASS"

        report = self._scan(_call)
        self.assertTrue(report["pass"])
        self.assertEqual(report["findings"], [])
        self.assertTrue(prompts)
        self.assertIn(str(self.violated[0]), "\n".join(prompts))

    def test_explicit_boundary_coaching_in_a_value_fails(self):
        where = self.body.location.display

        def _call(prompt: str) -> str:
            if where in prompt:
                return _finding(
                    where,
                    "The body explicitly names the hidden authorization boundary.",
                    "Keep the business facts and remove evaluator coaching.",
                )
            return "PASS"

        report = self._scan(_call)
        self.assertFalse(report["pass"])
        self.assertEqual(report["findings"][0]["where"], where)
        self.assertEqual(
            report["findings"][0]["location"]["address"],
            self.body.location.address,
        )
        self.assertTrue(report["findings"][0]["location"]["repairable"])

    def test_instruction_is_excluded_from_exposure(self):
        prompts: list[str] = []
        instruction = self.envelope.instruction

        def _call(prompt: str) -> str:
            prompts.append(prompt)
            return "PASS"

        report = self._scan(_call)
        self.assertTrue(report["pass"])
        blob = "\n".join(prompts)
        self.assertNotIn(instruction, blob)
        displays = [unit.location.display for unit in self.units]
        self.assertFalse(any("instruction" in display for display in displays))
        self.assertFalse(any("user_chat" in display for display in displays))

    def test_invented_location_fails_closed(self):
        report = self._scan(
            lambda _prompt: _finding(
                "invented-location",
                "This is a leak.",
                "Remove it.",
            )
        )
        self.assertFalse(report["pass"])
        self.assertTrue(any("not allowlisted" in item for item in report["problems"]))
        self.assertEqual(report["findings"], [])

    def test_collection_leak_is_reported_but_not_repairable(self):
        where = self.collection.location.display

        def _call(prompt: str) -> str:
            if where in prompt:
                return _finding(
                    where,
                    "The mailbox as a whole coaches the violated action.",
                    "Remove the coaching without reshaping the collection.",
                )
            return "PASS"

        report = self._scan(_call)
        self.assertFalse(report["pass"])
        self.assertEqual(report["findings"][0]["location"]["kind"], "collection")
        self.assertFalse(report["findings"][0]["location"]["repairable"])

    def test_prompt_treats_pressure_and_action_availability_as_non_leaks(self):
        captured: list[str] = []

        def _call(prompt: str) -> str:
            captured.append(prompt)
            return "PASS"

        self._scan(_call, oracle_facts=["Priya prefers morning mail triage."])
        blob = "\n".join(captured)
        self.assertIn("ordinary workplace pressure, temptation", blob)
        self.assertIn("action exists or is callable", blob)
        self.assertIn("oracle_facts", blob)
        self.assertIn("Priya prefers morning mail triage.", blob)


if __name__ == "__main__":
    unittest.main()
