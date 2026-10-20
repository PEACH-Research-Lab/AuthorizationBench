from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import (
    ServiceBase,
    fixture_env_var,
)
from authorization_eva_sandbox.services.multi_server import create_app
from services import _clock


class DummyService(ServiceBase):
    def __init__(self, service_id: str):
        self.items: list[str] = []
        super().__init__(service_id, "dummy")

    def load_data(self) -> None:
        self.items = ["seed"]

    def get_state(self) -> dict:
        return {"items": list(self.items)}


class ServiceBaseContractTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")

    def test_ids_audit_and_reset_are_deterministic(self):
        service = DummyService("work")
        service.load_data()
        first = service.new_id("message")
        second = service.new_id("message")
        self.assertNotEqual(first, second)

        service.log_action(
            "create_message",
            params={"subject": "hello"},
            source="agent",
            effect={"created_id": first},
        )
        entry = service.get_audit()[0]
        self.assertEqual(entry["sequence"], 1)
        self.assertEqual(entry["source"], "agent")
        self.assertEqual(entry["timestamp"], "2026-06-06T10:11:00")
        self.assertEqual(entry["effect"], {"created_id": first})

        service.items.append("changed")
        service.reset()
        self.assertEqual(service.get_state(), {"items": ["seed"]})
        self.assertEqual(service.get_audit(), [])
        self.assertEqual(service.new_id("message"), first)

    def test_standard_error_envelope(self):
        response = ServiceBase.error_response(
            code="NOT_FOUND",
            message="Message not found",
            status=404,
            details={"id": "missing"},
        )
        self.assertEqual(response.status_code, 404)
        payload = json.loads(response.body)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")
        self.assertEqual(payload["error"]["details"], {"id": "missing"})


class GlobalEnvironmentResetTests(unittest.TestCase):
    def test_global_reset_restores_clock_events_and_service_state(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td) / "email.json"
            fixture.write_text(json.dumps({
                "user_name": "Priya",
                "user_email": "priya@example.com",
                "emails": [],
            }))
            key = fixture_env_var("mail")
            previous = os.environ.get(key)
            os.environ[key] = str(fixture)
            try:
                app = create_app(
                    [{"id": "mail", "template": "email_client"}],
                    reference_time="2026-06-06T10:11:00Z",
                    grader_token="grader",
                )
                client = TestClient(app)
                headers = {"Authorization": "Bearer grader"}
                client.post(
                    "/_env/clock",
                    json={"advance_by": "2h"},
                    headers=headers,
                )
                client.post(
                    "/mail/messages/send",
                    json={
                        "to": ["team@example.com"],
                        "cc": [],
                        "bcc": [],
                        "subject": "Summary",
                        "body": "Done",
                        "attachments": [],
                    },
                )
                self.assertEqual(
                    len(client.get("/mail/audit", headers=headers).json()["actions"]),
                    1,
                )

                reset = client.post("/_env/reset", headers=headers)
                self.assertEqual(reset.status_code, 200)
                self.assertEqual(
                    reset.json()["narrative_time"],
                    "2026-06-06T10:11:00",
                )
                self.assertEqual(
                    client.get("/mail/audit", headers=headers).json()["actions"],
                    [],
                )
                state = client.get("/mail/state", headers=headers).json()["state"]
                self.assertEqual(state["emails"], [])
                events = client.get("/_env/events", headers=headers).json()
                self.assertEqual(events["fired"], [])
            finally:
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous


if __name__ == "__main__":
    unittest.main()
