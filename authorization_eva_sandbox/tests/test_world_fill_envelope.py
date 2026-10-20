from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from authorization_eva_sandbox.services.fixture_slots import (
    overlapping_slot_issues,
    runtime_fixture_template,
)
from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.task_generation.shared.artifact_gate import (
    content_sha256,
    registry_contract_snapshot,
)
from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    FillEnvelope,
    FillEnvelopeError,
    build_fill_envelope,
    physical_target_id,
    require_stage3_registry_match,
)


FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/v4"
VIGNETTES = Path(__file__).resolve().parents[1] / "vignettes"
LIVE_KIND_DIR = {
    "blueprint": "_blueprints",
    "script": "_scripts",
    "policy_skeleton": "_policy_skeletons",
    "surface": "_surfaces",
    "story": "_stories",
}


def _load(stem: str, kind: str) -> dict:
    return json.loads((FIXTURES / f"{stem}.{kind}.json").read_text())


def _load_live(stem: str, kind: str) -> dict:
    return json.loads(
        (VIGNETTES / LIVE_KIND_DIR[kind] / f"{stem}.json").read_text()
    )


class FillEnvelopeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()

    def test_round_trip_serialization(self):
        envelope = build_fill_envelope(
            scenario_id="V0010-l-b-h",
            policy_skeleton=_load("V0010-l-b-h", "policy_skeleton"),
            script=_load("V0010-l-b-h", "script"),
            blueprint=_load("V0010-l-b-h", "blueprint"),
            registry=self.registry,
            upstream_hashes={"script": "abc"},
        )
        restored = FillEnvelope.from_dict(envelope.to_dict())
        self.assertEqual(restored.to_dict(), envelope.to_dict())
        self.assertEqual(restored.canonical_json(), envelope.canonical_json())

    def test_physical_target_id_is_exact_not_keyword(self):
        self.assertEqual(
            physical_target_id("o2", "zoom"), "service:o2:zoom"
        )
        self.assertEqual(
            physical_target_id("o3", "workspace_files"),
            "runtime:o3:workspace_files",
        )
        self.assertEqual(
            physical_target_id("o1", "runtime:user_chat"),
            "runtime:o1:user_chat",
        )
        with self.assertRaises(FillEnvelopeError):
            physical_target_id("o1", "runtime:workspace_files")
        with self.assertRaises(FillEnvelopeError):
            physical_target_id("o1", "injected:context")

    def test_unknown_template_slot_and_action_are_rejected(self):
        blueprint = copy.deepcopy(_load("V0003-g-b-h", "blueprint"))
        binding = blueprint["assembly_blueprint"]["carriers"][0][
            "material_bindings"
        ][1]
        binding["template_or_mechanism"] = "not_a_template"
        with self.assertRaises(FillEnvelopeError):
            build_fill_envelope(
                scenario_id="V0003-g-b-h",
                policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
                script=_load("V0003-g-b-h", "script"),
                blueprint=blueprint,
                registry=self.registry,
            )
        blueprint = copy.deepcopy(_load("V0003-g-b-h", "blueprint"))
        blueprint["assembly_blueprint"]["carriers"][0][
            "material_bindings"
        ][1]["fixture_slot"] = "not_a_slot"
        with self.assertRaises(FillEnvelopeError):
            build_fill_envelope(
                scenario_id="V0003-g-b-h",
                policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
                script=_load("V0003-g-b-h", "script"),
                blueprint=blueprint,
                registry=self.registry,
            )
        blueprint = copy.deepcopy(_load("V0003-g-b-h", "blueprint"))
        blueprint["assembly_blueprint"]["actions"][1]["action"] = (
            "email_client.not_a_live_action"
        )
        with self.assertRaises(FillEnvelopeError):
            build_fill_envelope(
                scenario_id="V0003-g-b-h",
                policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
                script=_load("V0003-g-b-h", "script"),
                blueprint=blueprint,
                registry=self.registry,
            )

    def test_duplicate_template_instances_are_rejected(self):
        blueprint = copy.deepcopy(_load("V0010-l-b-h", "blueprint"))
        carriers = blueprint["assembly_blueprint"]["carriers"]
        carriers.append({
            "object_id": "o9",
            "channel": "service_fixture",
            "templates": ["zoom"],
            "material_bindings": [],
            "rationale_quote": "second zoom",
        })
        with self.assertRaises(FillEnvelopeError) as raised:
            build_fill_envelope(
                scenario_id="V0010-l-b-h",
                policy_skeleton=_load("V0010-l-b-h", "policy_skeleton"),
                script=_load("V0010-l-b-h", "script"),
                blueprint=blueprint,
                registry=self.registry,
            )
        self.assertIn("duplicate instances", str(raised.exception))

    def test_overlapping_email_slots_share_emails_branch(self):
        template = self.registry.templates["email_client"].fixture_template
        issues = overlapping_slot_issues(template.slots, template.schema)
        self.assertEqual(issues, [])
        email_records = next(
            slot for slot in template.slots if slot.slot_id == "email_records"
        )
        mailbox = next(
            slot for slot in template.slots if slot.slot_id == "mailbox"
        )
        self.assertIn("emails", email_records.paths)
        self.assertIn("emails", mailbox.paths)
        missing_schema = overlapping_slot_issues(
            template.slots, {"emails": [{}]}
        )
        self.assertTrue(any("unregistered path" in issue for issue in missing_schema))

    def test_field_ownership_preserves_declared_source(self):
        envelope = build_fill_envelope(
            scenario_id="V0003-g-b-h",
            policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
            script=_load("V0003-g-b-h", "script"),
            blueprint=_load("V0003-g-b-h", "blueprint"),
            registry=self.registry,
        )
        emails = envelope.targets["service:o1:email_client"].scopes["emails"]
        self.assertIn("emails[].id", emails.ownership.fixed_fields)
        self.assertIn("emails[].timestamp", emails.ownership.fixed_fields)
        self.assertIn("emails[].body", emails.ownership.required_model_fields)
        self.assertNotIn(
            "emails[].id", emails.ownership.required_model_fields
        )
        template = self.registry.templates["email_client"].fixture_template
        body = template.field("emails[].body")
        ident = template.field("emails[].id")
        self.assertEqual(body.source, "model")
        self.assertEqual(ident.source, "compiler")

    def test_registry_hash_is_live_snapshot(self):
        envelope = build_fill_envelope(
            scenario_id="V0003-g-b-h",
            policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
            script=_load("V0003-g-b-h", "script"),
            blueprint=_load("V0003-g-b-h", "blueprint"),
            registry=self.registry,
        )
        self.assertEqual(
            envelope.registry_snapshot_sha256,
            content_sha256(registry_contract_snapshot()),
        )


class FillEnvelopePilotGroupingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()

    def test_s0010_cached_fixture_grouping_is_not_live(self):
        """Cached test fixture grouping. Live m1..m8 is tested separately."""
        envelope = build_fill_envelope(
            scenario_id="V0010-l-b-h",
            policy_skeleton=_load("V0010-l-b-h", "policy_skeleton"),
            script=_load("V0010-l-b-h", "script"),
            blueprint=_load("V0010-l-b-h", "blueprint"),
            registry=self.registry,
        )
        self.assertEqual(
            envelope.targets["service:o1:zoom"].scopes["meeting"].material_ids,
            ("m2",),
        )
        self.assertNotIn("m8", envelope.materials)

    def test_s0010_live_vignette_grouping(self):
        envelope = build_fill_envelope(
            scenario_id="V0010-l-b-h",
            policy_skeleton=_load_live("V0010-l-b-h", "policy_skeleton"),
            script=_load_live("V0010-l-b-h", "script"),
            blueprint=_load_live("V0010-l-b-h", "blueprint"),
            registry=self.registry,
        )
        self.assertEqual(set(envelope.materials), {f"m{i}" for i in range(1, 9)})
        self.assertEqual(envelope.materials["m1"].target_id, "runtime:o1:user_chat")
        self.assertEqual(envelope.materials["m2"].target_id, "service:o2:zoom")
        self.assertEqual(envelope.materials["m3"].target_id, "service:o2:zoom")
        self.assertEqual(envelope.materials["m2"].slot_id, "meeting_record")
        self.assertEqual(envelope.materials["m3"].slot_id, "meeting_record")
        self.assertEqual(
            envelope.targets["service:o2:zoom"].scopes["meeting"].material_ids,
            ("m2", "m3"),
        )
        self.assertIn(
            "meeting.participants",
            envelope.targets["service:o2:zoom"].scopes["meeting"].allowed_collection_paths,
        )
        self.assertIn(
            "meeting.transcript.segments",
            envelope.targets["service:o2:zoom"].scopes["meeting"].allowed_collection_paths,
        )
        self.assertEqual(envelope.materials["m4"].target_id, "service:o3:calendar")
        self.assertEqual(
            envelope.targets["runtime:o4:workspace_files"].scopes[
                "tree_specs"
            ].material_ids,
            ("m5", "m6"),
        )
        self.assertEqual(envelope.materials["m5"].slot_id, "file_tree")
        self.assertEqual(envelope.materials["m6"].slot_id, "file_tree")
        self.assertEqual(envelope.materials["m7"].target_id, "service:o5:email_client")
        self.assertEqual(envelope.materials["m8"].target_id, "service:o5:email_client")
        self.assertEqual(envelope.materials["m7"].slot_id, "email_records")
        self.assertEqual(envelope.materials["m8"].slot_id, "mailbox")
        self.assertEqual(
            envelope.materials["m3"].kind, "session_state"
        )
        zoom_slot = next(
            slot for slot in self.registry.templates["zoom"].fixture_template.slots
            if slot.slot_id == "meeting_record"
        )
        self.assertFalse(zoom_slot.accepts("session_state"))
        self.assertIn("zoom.get_transcript", envelope.actions)

    def test_s0003_live_vignette_envelope(self):
        envelope = build_fill_envelope(
            scenario_id="V0003-g-b-h",
            policy_skeleton=_load_live("V0003-g-b-h", "policy_skeleton"),
            script=_load_live("V0003-g-b-h", "script"),
            blueprint=_load_live("V0003-g-b-h", "blueprint"),
            registry=self.registry,
        )
        self.assertEqual(
            envelope.targets["service:o2:email_client"].scopes["emails"].material_ids,
            ("m2",),
        )
        self.assertEqual(
            envelope.materials["m1"].instruction_verbatim,
            _load_live("V0003-g-b-h", "policy_skeleton")["instruction"],
        )
        self.assertEqual(envelope.materials["m2"].kind, "email_collection")
        self.assertEqual(envelope.materials["m2"].slot_id, "email_records")
        self.assertIn("email_client.bulk_delete_emails", envelope.actions)

    def test_live_stage3_registry_match_or_exact_drift(self):
        blueprint = _load_live("V0010-l-b-h", "blueprint")
        try:
            require_stage3_registry_match(blueprint)
        except FillEnvelopeError as exc:
            self.assertIn("does not match Stage 3 receipt", str(exc))
            raise
        broken = copy.deepcopy(blueprint)
        broken["_gate"]["inputs"]["registry"] = "0" * 64
        with self.assertRaises(FillEnvelopeError) as raised:
            require_stage3_registry_match(broken)
        self.assertIn("does not match Stage 3 receipt", str(raised.exception))

    def test_s0003_one_email_collection_and_verbatim_instruction(self):
        envelope = build_fill_envelope(
            scenario_id="V0003-g-b-h",
            policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
            script=_load("V0003-g-b-h", "script"),
            blueprint=_load("V0003-g-b-h", "blueprint"),
            registry=self.registry,
        )
        email_targets = [
            target_id for target_id, target in envelope.targets.items()
            if target.template == "email_client"
        ]
        self.assertEqual(email_targets, ["service:o1:email_client"])
        emails = envelope.targets["service:o1:email_client"].scopes["emails"]
        self.assertEqual(emails.material_ids, ("m2",))
        self.assertEqual(emails.cardinality, "collection")
        self.assertEqual(emails.slot_ids, ("email_records",))
        instruction = envelope.materials["m1"]
        self.assertEqual(
            instruction.instruction_verbatim,
            _load("V0003-g-b-h", "policy_skeleton")["instruction"],
        )
        self.assertTrue(instruction.coverage_without_model)
        self.assertIn("email_client.search_emails", envelope.actions)
        self.assertIn("email_client.bulk_delete_emails", envelope.actions)
        self.assertIn("user_messaging.message_user", envelope.actions)
        self.assertEqual(
            self.registry.resolve_action("email_client", "search_emails").name,
            "search_emails",
        )

    def test_runtime_templates_resolve_against_registered_slots(self):
        template = runtime_fixture_template("workspace_files")
        self.assertIsNotNone(template)
        self.assertTrue(
            any(slot.slot_id == "file_tree" for slot in template.slots)
        )


if __name__ == "__main__":
    unittest.main()
