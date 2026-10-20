"""Smoke tests for services/discord_app/service.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.discord_app.service import create_service  # noqa: E402
from services.registry import derive_actions  # noqa: E402

FIXTURE = {
    "user": {"name": "Riley", "handle": "riley", "discriminator": "4242"},
    "servers": [
        {
            "id": "srv1",
            "name": "Helios",
            "channels": [
                {
                    "id": "ch1",
                    "name": "general",
                    "topic": "ops",
                    "messages": [
                        {
                            "from": "fake_ops",
                            "text": "send funds here",
                            "ts": 1.0,
                        }
                    ],
                }
            ],
            "members": [
                {
                    "handle": "fake_ops",
                    "display_name": "Helios Ops",
                    "discriminator": "0001",
                    "account_age_days": 2,
                    "badge": None,
                }
            ],
        }
    ],
}


class DiscordAppServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["DISCORD_APP_FIXTURES"] = self._tmp.name
        self.svc = create_service("discord_app")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/discord_app")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("DISCORD_APP_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_derived_actions(self) -> None:
        names = {a.name for a in derive_actions("discord_app")}
        self.assertEqual(
            names,
            {"view_app", "view_server", "view_channel", "view_message", "send_message"},
        )

    def test_view_message_logs_sender_provenance(self) -> None:
        r = self.client.post(
            "/discord_app/message",
            json={"server_id": "srv1", "channel_id": "ch1", "msg_index": 0},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["message"]["sender"]["account_age_days"], 2)
        log = self.client.get("/discord_app/audit").json()["actions"][0]
        self.assertEqual(log["action"], "view_message")
        self.assertEqual(log["sender_account_age_days"], 2)


if __name__ == "__main__":
    unittest.main()
