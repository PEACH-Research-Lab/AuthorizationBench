"""Discord gaps borrowed from SmolClaw Discord / Slack reaction+member parity."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.discord_app.service import (
    DiscordAppService,
)
from authorization_eva_sandbox.services.registry import ServiceRegistry
from services import _clock


EXISTING_ACTIONS = frozenset({
    "view_app",
    "view_server",
    "view_channel",
    "view_message",
    "send_message",
})

NEW_ACTIONS = frozenset({
    "add_reaction",
    "list_members",
    "invite_member",
})


class DiscordExternalGapTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        fixture = Path(self.tempdir.name) / "discord.json"
        fixture.write_text(json.dumps({
            "app_name": "Discord",
            "user": {
                "name": "Priya",
                "handle": "priya",
                "discriminator": "4242",
                "avatar_color": "#5865f2",
            },
            "servers": [{
                "id": "S-helios",
                "name": "Helios Ops",
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
                "members": [
                    {"handle": "alice", "name": "Alice Johnson"},
                    {"handle": "bob", "name": "Bob Smith"},
                ],
                "roles": [],
            }],
        }))
        self.env_key = fixture_env_var("discord-external-gaps")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = DiscordAppService("discord-external-gaps")
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
        spec = ServiceRegistry.load(derive_live=True).templates["discord_app"]
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
            self.assertIn(name, DiscordAppService.ACTION_CONTRACTS)

    def test_nothing_removed_from_profiles(self):
        for profile in ("standard-v1", "full-v1"):
            actions = DiscordAppService.CAPABILITY_PROFILES[profile]
            self.assertEqual(
                set(actions),
                EXISTING_ACTIONS | NEW_ACTIONS,
            )
            self.assertEqual(len(actions), len(set(actions)))

    def test_mutation_contracts_declare_state_change(self):
        for name in ("add_reaction", "invite_member", "send_message"):
            contract = DiscordAppService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)
        read = DiscordAppService.ACTION_CONTRACTS["list_members"]
        self.assertEqual(read.state_effects, ())
        self.assertIn("read", read.capability_tags)
        self.assertNotIn("state_change", read.capability_tags)

    def test_list_members_is_read_only_and_audited(self):
        before = list(self.service.servers[0]["members"])
        response = self.client.post("/members/list", json={
            "server_id": "S-helios",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(
            [m["handle"] for m in body["members"]],
            ["alice", "bob"],
        )
        self.assertEqual(self.service.servers[0]["members"], before)
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "list_members")
        self.assertEqual(audit["member_count"], 2)

    def test_list_members_missing_server_is_audited(self):
        response = self.client.post("/members/list", json={
            "server_id": "missing",
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "SERVER_NOT_FOUND")
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "list_members")
        self.assertEqual(audit["status"], 404)

    def test_invite_member_appends_and_is_idempotent(self):
        first = self.client.post("/members/invite", json={
            "server_id": "S-helios",
            "handle": "dora",
            "name": "Dora Lee",
            "discriminator": "1111",
        })
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["status"], "invited")
        self.assertEqual(body["member"]["handle"], "dora")
        self.assertEqual(body["member"]["discriminator"], "1111")
        handles = [m["handle"] for m in self.service.servers[0]["members"]]
        self.assertEqual(handles, ["alice", "bob", "dora"])

        second = self.client.post("/members/invite", json={
            "server_id": "S-helios",
            "handle": "dora",
            "name": "Other",
        })
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "unchanged")
        self.assertEqual(
            [m["handle"] for m in self.service.servers[0]["members"]],
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
            "server_id": "S-helios",
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
            "server_id": "S-helios",
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

        stored = self.service.servers[0]["channels"][0]["messages"][0]["reactions"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["emoji"], "thumbsup")
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "add_reaction")
        self.assertEqual(audit["reaction"], "thumbsup")

    def test_add_reaction_errors_are_audited(self):
        missing_channel = self.client.post("/channel/react", json={
            "server_id": "S-helios",
            "channel_id": "missing",
            "message_ts": "1710000000.000100",
            "reaction": "eyes",
        })
        self.assertEqual(missing_channel.status_code, 404)
        self.assertEqual(
            missing_channel.json()["error"]["code"], "CHANNEL_NOT_FOUND"
        )

        missing_message = self.client.post("/channel/react", json={
            "server_id": "S-helios",
            "channel_id": "C-general",
            "message_ts": "0",
            "reaction": "eyes",
        })
        self.assertEqual(missing_message.status_code, 404)
        self.assertEqual(
            missing_message.json()["error"]["code"], "MESSAGE_NOT_FOUND"
        )

        empty = self.client.post("/channel/react", json={
            "server_id": "S-helios",
            "channel_id": "C-general",
            "message_ts": "1710000000.000100",
            "reaction": "  ",
        })
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()["error"]["code"], "INVALID_REACTION")

        for entry in self.service.get_audit()[-3:]:
            self.assertEqual(entry["action"], "add_reaction")
            self.assertIn(entry["status"], (404, 422))

    def test_existing_view_and_send_still_work(self):
        viewed = self.client.post("/app")
        self.assertEqual(viewed.status_code, 200)
        self.assertEqual(viewed.json()["app_name"], "Discord")

        sent = self.client.post("/send", json={
            "server_id": "S-helios",
            "channel_id": "C-general",
            "text": "ack",
        })
        self.assertEqual(sent.status_code, 200)
        self.assertTrue(sent.json()["ok"])
        self.assertEqual(
            self.service.get_audit()[-1]["action"],
            "send_message",
        )


if __name__ == "__main__":
    unittest.main()
