from __future__ import annotations

import json
import unittest
from pathlib import Path

from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    build_fill_envelope,
)
from authorization_eva_sandbox.task_generation.shared.world_realization import (
    compile_world_realization,
)
from authorization_eva_sandbox.task_generation.shared.world_verification import (
    CanonicalLocation,
    ReviewCondition,
    TargetDossier,
    allowlist_where,
    build_cleanup_prompt,
    build_target_dossier,
    parse_review_text,
    render_canonical_locations,
    review_compiled_world,
    review_conditions,
)
from authorization_eva_sandbox.tests.test_world_fill_envelope import _load
from authorization_eva_sandbox.tests.test_world_realization_contract import (
    joint_graph_to_realization,
)


def _envelope(registry, stem: str):
    return build_fill_envelope(
        scenario_id=stem,
        policy_skeleton=_load(stem, "policy_skeleton"),
        script=_load(stem, "script"),
        blueprint=_load(stem, "blueprint"),
        registry=registry,
    )


def _pass(_prompt: str) -> str:
    return "PASS"


def _finding(where: str, problem: str = "The required fact is missing.",
             suggestion: str = "Add the fact in ordinary language.") -> str:
    return (
        "FINDING\n"
        f"WHERE: {where}\n"
        f"PROBLEM: {problem}\n"
        f"SUGGESTION: {suggestion}\n"
        "END"
    )


def _synthetic_dossier() -> TargetDossier:
    target_id = "service:o1:email_client"
    conditions = [
        ReviewCondition("m2", 0, "local fact in one record", target_id),
        ReviewCondition("m2", 1, "collection is paginated", target_id),
        ReviewCondition("m2", 2, "search returns ordinary matches", target_id),
        ReviewCondition("m2", 3, "no volume narration", target_id),
    ]
    locations = [
        CanonicalLocation(target_id, "target", {"target_id": target_id}),
        CanonicalLocation(
            f"{target_id} > emails",
            "collection",
            {"target_id": target_id, "collection_path": "emails"},
        ),
        CanonicalLocation(
            f"{target_id} > emails > msg_06",
            "record",
            {
                "target_id": target_id,
                "collection_path": "emails",
                "record_key": "msg_06",
            },
        ),
        CanonicalLocation(
            f"{target_id} > emails > msg_06 > emails[].body",
            "field",
            {
                "target_id": target_id,
                "collection_path": "emails",
                "record_key": "msg_06",
                "field_path": "emails[].body",
            },
            repairable=True,
        ),
        CanonicalLocation(
            f"{target_id} > mailbox_label",
            "scalar",
            {"target_id": target_id, "field_path": "mailbox_label"},
            repairable=True,
        ),
        CanonicalLocation(
            "runtime:o3:workspace_files > /workspace/project/report.md",
            "file",
            {
                "target_id": "runtime:o3:workspace_files",
                "collection_path": "workspace_files",
                "record_key": "agenda",
                "file_key": "agenda",
                "field_path": "workspace_files[].content",
            },
            repairable=True,
        ),
        CanonicalLocation(
            f"{target_id} > action > email_client.list_messages",
            "action",
            {"target_id": target_id, "action_id": "email_client.list_messages"},
        ),
        CanonicalLocation(
            "event > vendor-followup",
            "event",
            {"event_key": "vendor-followup", "field_path": "$self"},
        ),
    ]
    return TargetDossier(
        target_id=target_id,
        conditions=conditions,
        locations=locations,
        prompt="",
        compiled={},
        location_by_display={item.display: item for item in locations},
        condition_by_id={item.condition_id: item for item in conditions},
    )


class WorldVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()
        cls.stem = "V0003-g-b-h"
        cls.envelope = _envelope(cls.registry, cls.stem)
        cls.realization = joint_graph_to_realization(
            _load(cls.stem, "joint_graph"), cls.envelope
        )
        (
            cls.fixtures,
            cls.runtime,
            cls.events,
            cls.reports,
            compile_issues,
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
        if compile_issues:
            raise AssertionError(f"V0003 compile failed: {compile_issues}")
        cls.email_target = "service:o1:email_client"
        cls.dossier = build_target_dossier(
            envelope=cls.envelope,
            realization=cls.realization,
            target_id=cls.email_target,
            registry=cls.registry,
            fixtures=cls.fixtures,
            runtime=cls.runtime,
            events=cls.events,
            compiler_report=cls.reports,
        )

    def test_pass_marks_unmentioned_conditions_passing(self):
        receipt = review_compiled_world(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            critic_call=_pass,
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            compiler_report=self.reports,
        )
        self.assertTrue(receipt["pass"])
        self.assertEqual(receipt["problems"], [])
        critic = [
            item for item in receipt["conditions"] if item["owner"] == "critic"
        ]
        self.assertTrue(critic)
        self.assertTrue(all(item["verdict"] == "pass" for item in critic))
        code = [
            item for item in receipt["conditions"] if item["owner"] == "code"
        ]
        self.assertTrue(code)
        self.assertTrue(all(item["verdict"] == "pass" for item in code))

    def test_valid_finding_maps_to_existing_address(self):
        field = next(
            item for item in self.dossier.locations if item.kind == "field"
        )
        condition = self.dossier.conditions[0]
        where = f"{condition.condition_id} @ {field.display}"

        def _call(prompt: str) -> str:
            self.assertIn(condition.condition_id, prompt)
            self.assertIn(field.display, prompt)
            return _finding(where)

        receipt = review_compiled_world(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            critic_call=_call,
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            compiler_report=self.reports,
        )
        self.assertFalse(receipt["pass"])
        mapped = receipt["targets"][0]["findings"][0]
        self.assertEqual(mapped["where"], where)
        self.assertEqual(mapped["location"]["address"], field.address)
        self.assertEqual(mapped["material_id"], condition.material_id)
        failed = [
            item for item in receipt["conditions"]
            if item["material_id"] == condition.material_id
            and item["condition_index"] == condition.condition_index
        ]
        self.assertEqual(failed[0]["verdict"], "fail")
        others = [
            item for item in receipt["conditions"]
            if item["owner"] == "critic"
            and (
                item["material_id"] != condition.material_id
                or item["condition_index"] != condition.condition_index
            )
        ]
        self.assertTrue(others)
        self.assertTrue(all(item["verdict"] == "pass" for item in others))

    def test_invented_location_fails_closed(self):
        condition = self.dossier.conditions[0]
        where = f"{condition.condition_id} @ invented-location"
        receipt = review_compiled_world(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            critic_call=lambda _prompt: _finding(where),
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            compiler_report=self.reports,
        )
        self.assertFalse(receipt["pass"])
        self.assertTrue(any("not allowlisted" in item for item in receipt["problems"]))

    def test_invented_condition_fails_closed(self):
        location = self.dossier.locations[0]
        where = f"m99[0] @ {location.display}"
        receipt = review_compiled_world(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            critic_call=lambda _prompt: _finding(where),
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            compiler_report=self.reports,
        )
        self.assertFalse(receipt["pass"])
        self.assertTrue(any("not allowlisted" in item for item in receipt["problems"]))

    def test_record_scalar_collection_and_file_locations_resolve(self):
        dossier = _synthetic_dossier()
        kinds = {
            "collection": f"m2[1] @ {dossier.location_by_display['service:o1:email_client > emails'].display}",
            "field": (
                "m2[0] @ service:o1:email_client > emails > msg_06 > emails[].body"
            ),
            "scalar": "m2[0] @ service:o1:email_client > mailbox_label",
            "file": (
                "m2[3] @ runtime:o3:workspace_files > /workspace/project/report.md"
            ),
        }
        raw = "\n".join(_finding(where) for where in kinds.values())
        status, findings, problems = parse_review_text(raw, dossier)
        self.assertEqual(problems, [])
        self.assertEqual(status, "findings")
        found = {item["location"]["kind"]: item for item in findings}
        self.assertEqual(found["field"]["location"]["address"]["record_key"], "msg_06")
        self.assertTrue(found["field"]["location"]["repairable"])
        self.assertEqual(found["collection"]["location"]["kind"], "collection")
        self.assertFalse(found["collection"]["location"]["repairable"])
        self.assertEqual(found["scalar"]["location"]["address"]["field_path"], "mailbox_label")
        self.assertEqual(
            found["file"]["location"]["address"]["file_key"], "agenda"
        )

    def test_local_collection_behavior_and_absence_use_generic_locations(self):
        dossier = _synthetic_dossier()
        raw = "\n".join([
            _finding("m2[0] @ service:o1:email_client > emails > msg_06 > emails[].body"),
            _finding("m2[1] @ service:o1:email_client > emails"),
            _finding("m2[2] @ service:o1:email_client > action > email_client.list_messages"),
            _finding("m2[3] @ service:o1:email_client"),
        ])
        status, findings, problems = parse_review_text(raw, dossier)
        self.assertEqual(status, "findings")
        self.assertEqual(problems, [])
        kinds = [item["location"]["kind"] for item in findings]
        self.assertEqual(kinds, ["field", "collection", "action", "target"])
        source = Path(
            __file__
        ).resolve().parents[1] / "task_generation/shared/world_verification.py"
        text = source.read_text()
        self.assertNotIn("total_matches", text)
        self.assertNotIn("30 emails", text)
        self.assertNotIn("gmail", text.lower())

    def test_cleanup_reformats_without_changing_verdict(self):
        field = next(
            item for item in self.dossier.locations if item.kind == "field"
        )
        condition = self.dossier.conditions[4]
        where = f"{condition.condition_id} @ {field.display}"
        cleaned = _finding(where)

        def _messy(_prompt: str) -> str:
            return f"Here is the review.\n{cleaned}\nThanks."

        def _clean(prompt: str) -> str:
            self.assertIn("Here is the review", prompt)
            self.assertIn(where, allowlist_where(self.dossier))
            return cleaned

        receipt = review_compiled_world(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            critic_call=_messy,
            cleanup_call=_clean,
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            compiler_report=self.reports,
        )
        self.assertFalse(receipt["pass"])
        self.assertEqual(receipt["targets"][0]["cleanup_reply"], cleaned)
        self.assertEqual(receipt["targets"][0]["findings"][0]["where"], where)

    def test_cleanup_does_not_invent_a_location(self):
        receipt = review_compiled_world(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            critic_call=lambda _prompt: "FINDING\nWHERE: missing\nEND",
            cleanup_call=lambda _prompt: _finding(
                f"{self.dossier.conditions[0].condition_id} @ invented"
            ),
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            compiler_report=self.reports,
        )
        self.assertFalse(receipt["pass"])
        self.assertTrue(any("not allowlisted" in item for item in receipt["problems"]))
        self.assertEqual(receipt["targets"][0]["findings"], [])

    def test_pass_cannot_coexist_with_findings(self):
        status, findings, problems = parse_review_text(
            "PASS\n" + _finding(
                f"{self.dossier.conditions[0].condition_id} @ "
                f"{self.dossier.locations[0].display}"
            ),
            self.dossier,
        )
        self.assertEqual(status, "error")
        self.assertEqual(findings, [])
        self.assertTrue(any("coexist" in item for item in problems))

    def test_instruction_is_code_owned_and_not_sent_to_critic(self):
        instruction = [
            item for item in review_conditions(self.envelope) if item.owner == "code"
        ]
        self.assertTrue(instruction)
        self.assertNotIn(
            instruction[0].condition_id,
            self.dossier.prompt,
        )
        calls: list[str] = []

        def _track(prompt: str) -> str:
            calls.append(prompt)
            return "PASS"

        receipt = review_compiled_world(
            envelope=self.envelope,
            realization=self.realization,
            registry=self.registry,
            critic_call=_track,
            fixtures=self.fixtures,
            runtime=self.runtime,
            events=self.events,
            compiler_report=self.reports,
        )
        self.assertTrue(receipt["pass"])
        self.assertEqual(len(calls), 1)
        self.assertIn(self.email_target, calls[0])
        self.assertNotIn("runtime:o1:user_chat", calls[0])

    def test_prompt_lists_conditions_not_each_generated_record_as_the_task(self):
        self.assertIn("Judge EVERY listed Stage 2 condition", self.dossier.prompt)
        self.assertIn("Do not grade incidental generated records", self.dossier.prompt)
        self.assertIn("m2[0]:", self.dossier.prompt)
        self.assertIn(self.email_target, self.dossier.prompt)
        self.assertIn("emails", json.dumps(self.dossier.compiled))

    def test_v0010_renders_scalar_and_file_locations(self):
        envelope = _envelope(self.registry, "V0010-l-b-h")
        realization = joint_graph_to_realization(
            _load("V0010-l-b-h", "joint_graph"), envelope
        )
        zoom = next(
            target_id for target_id, target in envelope.targets.items()
            if target.template == "zoom"
        )
        zoom_locations = render_canonical_locations(
            envelope=envelope,
            realization=realization,
            target_id=zoom,
            registry=self.registry,
        )
        self.assertTrue(any(item.kind == "scalar" for item in zoom_locations))
        files = next(
            target_id for target_id, target in envelope.targets.items()
            if target.template == "workspace_files"
        )
        file_locations = render_canonical_locations(
            envelope=envelope,
            realization=realization,
            target_id=files,
            registry=self.registry,
        )
        self.assertTrue(
            any(item.kind in {"file", "record", "field"} for item in file_locations)
        )
        receipt = review_compiled_world(
            envelope=envelope,
            realization=realization,
            registry=self.registry,
            critic_call=_pass,
        )
        self.assertTrue(receipt["pass"])

    def test_cleanup_prompt_lists_condition_ids_and_locations(self):
        prompt = build_cleanup_prompt("bad", self.dossier)
        self.assertIn(self.dossier.conditions[0].condition_id, prompt)
        self.assertIn(self.dossier.locations[0].display, prompt)


if __name__ == "__main__":
    unittest.main()
