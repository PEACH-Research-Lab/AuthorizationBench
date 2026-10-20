"""Email FixtureTemplate pilot: contract, runtime behavior, and V4 bulk hooks."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import (
    fixture_env_var,
    validate_fixture,
)
from authorization_eva_sandbox.services.email_client.service import (
    EmailClientService,
    create_service,
)
from authorization_eva_sandbox.services.registry import ServiceRegistry


class EmailFixtureTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = ServiceRegistry.load().templates["email_client"]
        cls.template = cls.spec.fixture_template

    def test_email_uses_only_canonical_template_declaration(self):
        self.assertTrue(hasattr(EmailClientService, "FIXTURE_TEMPLATE"))
        self.assertFalse(hasattr(EmailClientService, "FIXTURE_SCHEMA"))
        self.assertFalse(hasattr(EmailClientService, "FIXTURE_SLOTS"))
        self.assertTrue(self.template.migrated)

    def test_every_field_has_source_meaning_and_format(self):
        self.assertTrue(self.template.fields)
        for field in self.template.fields:
            self.assertTrue(field.source, field.path)
            self.assertTrue(field.meaning, field.path)
            self.assertTrue(field.format, field.path)

    def test_email_slots_are_generic_not_seed_specific(self):
        slots = {slot.slot_id: slot for slot in self.template.slots}
        self.assertEqual(
            {
                "email_records", "mailbox", "mailbox_settings",
                "auto_completion", "scripted_replies",
            }.issubset(slots),
            True,
        )
        self.assertEqual(slots["email_records"].bulk_generator, "email_records")
        self.assertEqual(slots["email_records"].cardinality, "scenario")
        self.assertEqual(slots["mailbox"].bulk_generator, "email_records")
        serialized = json.dumps(self.template.to_dict()).lower()
        for seed_specific in ("s0003", "delete old email", "meeting gossip"):
            self.assertNotIn(seed_specific, serialized)

    def test_schema_rejects_noncanonical_folder_and_priority(self):
        schema = self.template.schema
        fixture = {
            "emails": [{
                "from": "offers@example.com",
                "to": ["user@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Offer",
                "body": "Promotion",
                "folder": "Inbox",
                "priority": "urgent",
            }],
        }
        issues = validate_fixture(fixture, schema)
        self.assertTrue(any("folder" in issue for issue in issues))
        self.assertTrue(any("priority" in issue for issue in issues))


class EmailFixtureTemplateRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({
            "user_name": "Priya",
            "user_email": "priya@example.com",
            "emails": [{
                "id": "mail-1",
                "thread_id": "thread-1",
                "from": "Store <offers@store.example>",
                "to": ["priya@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Seasonal promotion",
                "body": "Save twenty percent this week.",
                "folder": "inbox",
                "read": True,
                "starred": False,
                "priority": "normal",
                "timestamp": "2026-06-01T09:00:00Z",
                "created_at": "2026-06-01T09:00:00Z",
                "updated_at": "2026-06-01T09:00:00Z",
                "reply_to": "",
                "attachments": [],
            }],
        }, self.temp)
        self.temp.close()
        self.service_id = "email_template_pilot"
        os.environ[fixture_env_var(self.service_id)] = self.temp.name
        self.service = create_service(self.service_id)
        self.service.load_data()
        app = FastAPI()
        app.include_router(self.service.router, prefix="/mail")
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop(fixture_env_var(self.service_id), None)
        Path(self.temp.name).unlink(missing_ok=True)

    def test_template_fixture_lists_reads_and_archives(self):
        listed = self.client.post(
            "/mail/messages/list", json={"folder": "inbox"}
        ).json()
        self.assertEqual(listed["messages"][0]["id"], "mail-1")
        read = self.client.post(
            "/mail/messages/get", json={"message_id": "mail-1"}
        ).json()
        self.assertEqual(
            read["message"]["body"], "Save twenty percent this week."
        )
        archived = self.client.post(
            "/mail/messages/modify",
            json={"message_id": "mail-1", "folder": "archive"},
        ).json()
        self.assertEqual(archived["status"], "modified")
        self.assertEqual(
            next(iter(self.service.emails.values()))["folder"], "archive"
        )

    def test_actions_are_auditable(self):
        self.client.post("/mail/messages/list", json={"folder": "inbox"})
        self.client.post(
            "/mail/messages/get", json={"message_id": "mail-1"}
        )
        actions = [
            item["action"]
            for item in self.client.get("/mail/audit").json()["actions"]
        ]
        self.assertEqual(actions, ["list_messages", "get_message"])


if __name__ == "__main__":
    unittest.main()
