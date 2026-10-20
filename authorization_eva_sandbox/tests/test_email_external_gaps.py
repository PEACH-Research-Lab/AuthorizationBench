"""Contact-search gap coverage for email_client (AgentDojo-style address book)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.email_client.service import (
    EmailClientService,
)
from authorization_eva_sandbox.services.registry import ServiceRegistry


EXISTING_ACTIONS = frozenset({
    "list_folders", "list_messages", "get_message", "get_thread",
    "get_today_digest", "get_mailbox_stats", "bulk_delete_emails",
    "send_message", "create_draft", "update_draft", "send_draft",
    "delete_draft", "modify_message", "bulk_modify_messages",
    "list_emails", "read_email", "search_emails", "send_email",
    "save_draft", "archive_email", "toggle_star", "delete_email",
    "get_sending_settings", "update_sending_settings",
})

NEW_CONTACT_ACTIONS = frozenset({
    "search_contacts_by_name",
    "search_contacts_by_email",
})


class EmailContactSearchGapTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        fixture = Path(self.tempdir.name) / "email.json"
        fixture.write_text(json.dumps({
            "user_name": "Priya",
            "user_email": "priya@example.com",
            "emails": [],
            "contacts": [
                {"name": "Alice Johnson", "email": "alice@northbridge.com"},
                {"name": "Bob Smith", "email": "bob@example.org"},
                {"name": "Alice Nguyen", "email": "alice.nguyen@labs.io"},
            ],
        }))
        self.env_key = fixture_env_var("mail")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = EmailClientService("mail")
        self.service.load_data()
        app = FastAPI()
        app.include_router(self.service.router)
        self.client = TestClient(app)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop(self.env_key, None)
        else:
            os.environ[self.env_key] = self.previous
        self.tempdir.cleanup()

    def test_existing_actions_remain_registered(self):
        spec = ServiceRegistry.load(derive_live=True).templates["email_client"]
        live = {action.name for action in spec.actions}
        self.assertTrue(EXISTING_ACTIONS.issubset(live))
        self.assertTrue(NEW_CONTACT_ACTIONS.issubset(live))

        prior_core = frozenset({
            "list_folders", "list_messages", "get_message", "get_thread",
            "send_message", "create_draft", "update_draft", "send_draft",
            "delete_draft", "modify_message", "bulk_modify_messages",
        })
        standard = set(spec.capability_profiles["standard-v1"])
        full = set(spec.capability_profiles["full-v1"])
        self.assertTrue(prior_core.issubset(standard))
        self.assertTrue(NEW_CONTACT_ACTIONS.issubset(standard))
        self.assertTrue(EXISTING_ACTIONS.issubset(full))
        self.assertTrue(NEW_CONTACT_ACTIONS.issubset(full))

    def test_contracts_are_core_reads_without_state_change(self):
        contracts = EmailClientService.ACTION_CONTRACTS
        for name in NEW_CONTACT_ACTIONS:
            contract = contracts[name]
            self.assertEqual(contract.classification, "core")
            self.assertEqual(contract.state_effects, ())
            self.assertIn("read", contract.capability_tags)
            self.assertNotIn("state_change", contract.capability_tags)

    def test_search_contacts_by_name_is_case_insensitive_substring(self):
        response = self.client.post(
            "/contacts/search_by_name", json={"query": "alice"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total_matches"], 2)
        self.assertEqual(
            [item["email"] for item in payload["contacts"]],
            ["alice@northbridge.com", "alice.nguyen@labs.io"],
        )
        self.assertEqual(
            self.service.get_audit()[-1]["action"],
            "search_contacts_by_name",
        )
        self.assertEqual(self.service.get_audit()[-1]["total_matches"], 2)

    def test_search_contacts_by_email_is_case_insensitive_substring(self):
        response = self.client.post(
            "/contacts/search_by_email", json={"query": "EXAMPLE.ORG"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total_matches"], 1)
        self.assertEqual(payload["contacts"], [{
            "name": "Bob Smith",
            "email": "bob@example.org",
        }])

    def test_empty_matches_are_audited_and_non_mutating(self):
        before = self.service.get_state()["contacts"]
        response = self.client.post(
            "/contacts/search_by_name", json={"query": "zzz-missing"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "contacts": [],
            "total_matches": 0,
        })
        self.assertEqual(self.service.get_state()["contacts"], before)
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "search_contacts_by_name")
        self.assertEqual(audit["total_matches"], 0)

    def test_contacts_exposed_in_evaluator_state(self):
        state = self.service.get_state()
        self.assertEqual(len(state["contacts"]), 3)
        self.assertEqual(state["contacts"][0]["email"], "alice@northbridge.com")

    def test_fixture_template_declares_contacts(self):
        template = EmailClientService.FIXTURE_TEMPLATE
        self.assertIn("contacts", template["schema"])
        self.assertIn("contacts[].name", template["fields"])
        self.assertIn("contacts[].email", template["fields"])
        self.assertIn("contacts", template["materialization_slots"])


if __name__ == "__main__":
    unittest.main()
