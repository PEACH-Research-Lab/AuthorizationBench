"""Offline mechanical conformance for canonical Email, Calendar, and Meeting services."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from authorization_eva_sandbox.services.fixture_slots import (
    schema_leaf_paths,
    validate_fixture_slot,
    validate_fixture_template,
)
from authorization_eva_sandbox.services.registry import (
    ServiceRegistry,
    derive_actions,
    validate_catalog_data,
)

CATALOG_PATH = Path(__file__).resolve().parents[1] / "services/service_catalog.json"

STANDARD_TEMPLATES = ("email_client", "calendar", "zoom")

EMAIL_CORE_ACTIONS = frozenset({
    "list_folders",
    "list_messages",
    "get_message",
    "get_thread",
    "send_message",
    "create_draft",
    "update_draft",
    "send_draft",
    "delete_draft",
    "modify_message",
    "bulk_modify_messages",
    "search_contacts_by_name",
    "search_contacts_by_email",
})

EMAIL_EXTENSION_ACTIONS = frozenset({
    "get_today_digest",
    "get_mailbox_stats",
    "bulk_delete_emails",
    "list_emails",
    "read_email",
    "search_emails",
    "send_email",
    "save_draft",
    "archive_email",
    "toggle_star",
    "delete_email",
    "get_sending_settings",
    "update_sending_settings",
})

CALENDAR_CORE_ACTIONS = frozenset({
    "list_calendars",
    "list_events",
    "get_event",
    "create_event",
    "update_event",
    "cancel_event",
    "delete_event",
    "list_event_instances",
    "query_free_busy",
    "add_event_participants",
})

CALENDAR_EXTENSION_ACTIONS = frozenset({
    "view_day",
    "view_month",
    "decline_pending_request",
    "send_calendar_message",
})

MEETING_CORE_ACTIONS = frozenset({
    "get_meeting",
    "accept_invitation",
    "decline_invitation",
    "leave_meeting",
    "list_participants",
    "start_recording",
    "stop_recording",
    "start_transcription",
    "stop_transcription",
    "get_transcript",
    "get_summary",
    "create_summary_email_draft",
})

MEETING_EXTENSION_ACTIONS = frozenset({
    "view_desktop",
    "join_meeting",
    "decline_meeting",
    "continue_recording",
    "open_summary_in_email",
    "view_settings",
    "toggle_setting",
    "api_set_settings",
})

ENVIRONMENT_ONLY_ACTIONS = frozenset({
    "conference_start",
    "conference_end",
    "participant_join",
    "participant_leave",
    "append_transcript",
    "summary_ready",
    "recording_ready",
    "transcription_ready",
    "ringing_timed_out",
})

CORE_BY_TEMPLATE = {
    "email_client": EMAIL_CORE_ACTIONS,
    "calendar": CALENDAR_CORE_ACTIONS,
    "zoom": MEETING_CORE_ACTIONS,
}

EXTENSION_BY_TEMPLATE = {
    "email_client": EMAIL_EXTENSION_ACTIONS,
    "calendar": CALENDAR_EXTENSION_ACTIONS,
    "zoom": MEETING_EXTENSION_ACTIONS,
}


def _action_names(template: str) -> frozenset[str]:
    return frozenset(action.name for action in derive_actions(template))


def _standard_snapshot() -> dict[str, dict]:
    registry = ServiceRegistry.load()
    snapshot: dict[str, dict] = {}
    for template in STANDARD_TEMPLATES:
        spec = registry.templates[template]
        snapshot[template] = {
            "actions": [action.to_dict() for action in spec.actions],
            "fixture_template": spec.fixture_template.to_dict(),
        }
    return snapshot


class StandardServiceOpenApiConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()

    def test_openapi_operation_ids_are_unique_per_service(self):
        for template in STANDARD_TEMPLATES:
            names = [action.name for action in self.registry.templates[template].actions]
            with self.subTest(template=template):
                self.assertEqual(len(names), len(set(names)))

    def test_derive_actions_matches_registry_inventory(self):
        for template in STANDARD_TEMPLATES:
            derived = sorted(action.name for action in derive_actions(template))
            registered = sorted(
                action.name for action in self.registry.templates[template].actions
            )
            with self.subTest(template=template):
                self.assertEqual(derived, registered)


class StandardServiceActionInventoryTests(unittest.TestCase):
    def test_required_core_actions_are_present(self):
        for template, core in CORE_BY_TEMPLATE.items():
            names = _action_names(template)
            with self.subTest(template=template):
                self.assertTrue(core.issubset(names), sorted(core - names))

    def test_extension_actions_remain_present(self):
        for template, extensions in EXTENSION_BY_TEMPLATE.items():
            names = _action_names(template)
            with self.subTest(template=template):
                self.assertTrue(
                    extensions.issubset(names), sorted(extensions - names)
                )

    def test_inventory_partitions_into_core_and_extensions(self):
        for template in STANDARD_TEMPLATES:
            names = _action_names(template)
            core = CORE_BY_TEMPLATE[template]
            extensions = EXTENSION_BY_TEMPLATE[template]
            with self.subTest(template=template):
                self.assertEqual(names, core | extensions)
                self.assertFalse(core & extensions)

    def test_environment_only_actions_are_not_in_openapi_inventory(self):
        for template in STANDARD_TEMPLATES:
            names = _action_names(template)
            with self.subTest(template=template):
                self.assertFalse(names & ENVIRONMENT_ONLY_ACTIONS)

    def test_every_live_action_has_one_semantic_contract(self):
        registry = ServiceRegistry.load()
        for template in STANDARD_TEMPLATES:
            actions = registry.templates[template].actions
            with self.subTest(template=template):
                self.assertTrue(actions)
                self.assertTrue(all(action.contract for action in actions))
                self.assertTrue(all(
                    action.contract.output_schema for action in actions
                ))

    def test_capability_profiles_are_stable_and_partitioned(self):
        registry = ServiceRegistry.load()
        for template in STANDARD_TEMPLATES:
            spec = registry.templates[template]
            with self.subTest(template=template):
                self.assertEqual(
                    set(spec.capability_profiles),
                    {"standard-v1", "full-v1"},
                )
                self.assertEqual(
                    set(spec.capability_profiles["standard-v1"]),
                    CORE_BY_TEMPLATE[template],
                )
                self.assertEqual(
                    set(spec.capability_profiles["full-v1"]),
                    CORE_BY_TEMPLATE[template]
                    | EXTENSION_BY_TEMPLATE[template],
                )

    def test_environment_contracts_are_separate_from_profiles(self):
        registry = ServiceRegistry.load()
        meeting = registry.templates["zoom"]
        self.assertEqual(
            set(meeting.environment_actions), ENVIRONMENT_ONLY_ACTIONS
        )
        exposed = {
            action
            for actions in meeting.capability_profiles.values()
            for action in actions
        }
        self.assertFalse(exposed & set(meeting.environment_actions))


class StandardServiceFixtureTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()

    def test_every_standard_fixture_template_is_migrated(self):
        for template in STANDARD_TEMPLATES:
            fixture_template = self.registry.templates[template].fixture_template
            with self.subTest(template=template):
                self.assertIsNotNone(fixture_template)
                self.assertTrue(fixture_template.migrated)

    def test_every_standard_fixture_template_validates_structurally(self):
        for template in STANDARD_TEMPLATES:
            fixture_template = self.registry.templates[template].fixture_template
            with self.subTest(template=template):
                self.assertEqual(validate_fixture_template(fixture_template), [])

    def test_compiler_fields_belong_to_template_schema(self):
        for template in STANDARD_TEMPLATES:
            fixture_template = self.registry.templates[template].fixture_template
            schema = fixture_template.schema
            leaves = set(schema_leaf_paths(schema))
            with self.subTest(template=template):
                for slot in fixture_template.slots:
                    self.assertEqual(
                        validate_fixture_slot(slot, schema), [],
                        slot.slot_id,
                    )
                    for compiler_field in slot.compiler_fields:
                        self.assertIn(compiler_field, leaves, compiler_field)


class StandardServiceRegistryDeterminismTests(unittest.TestCase):
    def test_live_derivation_is_deterministic_across_two_loads(self):
        first = _standard_snapshot()
        second = _standard_snapshot()
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )


class ActiveServiceCatalogV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG_PATH.read_text())

    def test_catalog_is_canonical_v4_and_valid(self):
        self.assertEqual(
            self.catalog["schema_version"], "v4-service-catalog-1"
        )
        self.assertEqual(validate_catalog_data(self.catalog), [])
        self.assertEqual(len(self.catalog["templates"]), 37)

    def test_catalog_has_no_legacy_fields_or_unimplemented_templates(self):
        forbidden = {
            "actions", "actions_v1_partial", "actions_inventory_incomplete",
            "converted", "conversion_batch", "source_v1", "synthesis_spec",
            "synthesized", "notes", "bespoke_mechanics",
            "affordance_candidates",
        }
        removed = {
            "customer_support_console", "class_trip_form",
            "identity_survey", "content_site",
        }
        names = {
            item["template"] for item in self.catalog["templates"]
        }
        self.assertFalse(names & removed)
        for item in self.catalog["templates"]:
            self.assertFalse(set(item) & forbidden, item["template"])

    def test_every_capability_note_references_live_actions(self):
        registry = ServiceRegistry.load()
        for item in self.catalog["templates"]:
            live = {
                action.name
                for action in registry.templates[item["template"]].actions
            }
            for note in item.get("capability_notes") or []:
                self.assertTrue(set(note["action_refs"]).issubset(live))


if __name__ == "__main__":
    unittest.main()
