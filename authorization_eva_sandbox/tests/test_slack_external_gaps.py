"""Slack gaps borrowed from AgentDojo / SmolClaw / Agent-Diff Slack suites."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.slack_workspace.service import (
    SlackWorkspaceService,
)
from authorization_eva_sandbox.services.registry import ServiceRegistry
from services import _clock


EXISTING_ACTIONS = frozenset({
    "list_conversations",
    "read_channel",
    "read_dm",
    "send_dm",
    "send_channel_message",
})

NEW_ACTIONS = frozenset({
    "add_reaction",
    "list_members",
    "invite_member",
})


class SlackExternalGapTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        fixture = Path(self.tempdir.name) / "slack.json"
        fixture.write_text(json.dumps({
            "workspace_name": "Northbridge",
            "user": {"name": "Priya", "handle": "priya"},
            "members": [
                {"handle": "alice", "name": "Alice Johnson", "title": "PM"},
                {"handle": "bob", "name": "Bob Smith", "title": "Engineer"},
            ],
            "channels": [{
                "id": "C-general",
                "name": "general",
                "topic": "Company-wide",
                "messages": [{
                    "from": "alice",
                    "text": "Ship notes are ready.",
                    "ts": "1710000000.000100",
                }],
            }],
            "dms": [{
                "with": "alice",
                "messages": [],
            }],
        }))
        self.env_key = fixture_env_var("slack-external-gaps")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = SlackWorkspaceService("slack-external-gaps")
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
        spec = ServiceRegistry.load(derive_live=True).templates["slack_workspace"]
        live = {action.name for action in spec.actions}
        self.assertTrue(EXISTING_ACTIONS.issubset(live))
        self.assertTrue(NEW_ACTIONS.issubset(live))

        standard = set(spec.capability_profiles["standard-v1"])
        full = set(spec.capability_profiles["full-v1"])
        self.assertTrue(EXISTING_ACTIONS.issubset(standard))
        self.assertTrue(NEW_ACTIONS.issubset(standard))
        self.assertTrue(EXISTING_ACTIONS.issubset(full))
        self.assertTrue(NEW_ACTIONS.issubset(full))

        for name in EXISTING_ACTIONS | NEW_ACTIONS:
            self.assertIn(name, SlackWorkspaceService.ACTION_CONTRACTS)

    def test_nothing_removed_from_profiles(self):
        for profile in ("standard-v1", "full-v1"):
            actions = SlackWorkspaceService.CAPABILITY_PROFILES[profile]
            self.assertEqual(
                set(actions),
                EXISTING_ACTIONS | NEW_ACTIONS,
            )
            self.assertEqual(len(actions), len(set(actions)))

    def test_mutation_contracts_declare_state_change(self):
        for name in ("add_reaction", "invite_member", "send_dm",
                     "send_channel_message"):
            contract = SlackWorkspaceService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)
        read = SlackWorkspaceService.ACTION_CONTRACTS["list_members"]
        self.assertEqual(read.state_effects, ())
        self.assertIn("read", read.capability_tags)
        self.assertNotIn("state_change", read.capability_tags)

    def test_list_members_is_read_only_and_audited(self):
        before = list(self.service.get_state()["members"])
        response = self.client.post("/members/list")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(
            [m["handle"] for m in body["members"]],
            ["alice", "bob"],
        )
        self.assertEqual(self.service.get_state()["members"], before)
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "list_members")
        self.assertEqual(audit["member_count"], 2)

    def test_invite_member_appends_and_is_idempotent(self):
        first = self.client.post("/members/invite", json={
            "handle": "dora",
            "name": "Dora Lee",
            "title": "Designer",
            "email": "dora@example.com",
        })
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["status"], "invited")
        self.assertEqual(body["member"]["handle"], "dora")
        self.assertEqual(body["member"]["email"], "dora@example.com")
        handles = [m["handle"] for m in self.service.get_state()["members"]]
        self.assertEqual(handles, ["alice", "bob", "dora"])

        second = self.client.post("/members/invite", json={
            "handle": "dora",
            "name": "Other",
        })
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "unchanged")
        self.assertEqual(
            [m["handle"] for m in self.service.get_state()["members"]],
            ["alice", "bob", "dora"],
        )
        audit = self.service.get_audit()
        self.assertEqual(
            [entry["action"] for entry in audit[-2:]],
            ["invite_member", "invite_member"],
        )
        self.assertTrue(audit[-1]["no_op"])

    def test_invite_member_rejects_empty_handle(self):
        response = self.client.post("/members/invite", json={
            "handle": "   ",
            "name": "Nobody",
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "INVALID_HANDLE")
        self.assertEqual(
            self.service.get_audit()[-1]["action"],
            "invite_member",
        )
        self.assertEqual(self.service.get_audit()[-1]["status"], 422)

    def test_add_reaction_appends_to_message(self):
        response = self.client.post("/channel/react", json={
            "channel_id": "C-general",
            "message_ts": "1710000000.000100",
            "reaction": "thumbsup",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        reaction = body["reaction"]
        self.assertEqual(reaction["emoji"], "thumbsup")
        self.assertEqual(reaction["from"], "priya")
        self.assertEqual(reaction["ts"], self.service.now_iso())
        self.assertTrue(str(reaction["id"]).startswith("reaction-"))

        stored = self.service.channels[0]["messages"][0]["reactions"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["emoji"], "thumbsup")
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "add_reaction")
        self.assertEqual(audit["reaction"], "thumbsup")

    def test_add_reaction_errors_are_audited(self):
        missing_channel = self.client.post("/channel/react", json={
            "channel_id": "missing",
            "message_ts": "1710000000.000100",
            "reaction": "eyes",
        })
        self.assertEqual(missing_channel.status_code, 404)
        self.assertEqual(
            missing_channel.json()["error"]["code"], "CHANNEL_NOT_FOUND"
        )

        missing_message = self.client.post("/channel/react", json={
            "channel_id": "C-general",
            "message_ts": "0",
            "reaction": "eyes",
        })
        self.assertEqual(missing_message.status_code, 404)
        self.assertEqual(
            missing_message.json()["error"]["code"], "MESSAGE_NOT_FOUND"
        )

        empty = self.client.post("/channel/react", json={
            "channel_id": "C-general",
            "message_ts": "1710000000.000100",
            "reaction": "  ",
        })
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()["error"]["code"], "INVALID_REACTION")

        for entry in self.service.get_audit()[-3:]:
            self.assertEqual(entry["action"], "add_reaction")
            self.assertIn(entry["status"], (404, 422))

    def test_existing_send_and_list_still_work(self):
        listed = self.client.post("/conversations")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["workspace_name"], "Northbridge")

        sent = self.client.post("/channel/send", json={
            "channel_id": "C-general",
            "text": "ack",
        })
        self.assertEqual(sent.status_code, 200)
        self.assertTrue(sent.json()["ok"])
        self.assertEqual(
            self.service.get_audit()[-1]["action"],
            "send_channel_message",
        )


if __name__ == "__main__":
    unittest.main()
