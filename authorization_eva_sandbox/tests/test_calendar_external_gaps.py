"""Calendar gaps borrowed from external benchmarks (AgentDojo participants)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.calendar.service import CalendarService
from services import _clock


_BASELINE_CORE = (
    "list_calendars", "list_events", "get_event", "create_event",
    "update_event", "cancel_event", "delete_event",
    "list_event_instances", "query_free_busy",
)
_BASELINE_EXTENSION = (
    "view_day", "view_month", "decline_pending_request",
    "send_calendar_message",
)


class CalendarExternalGapTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        fixture = Path(self.tempdir.name) / "calendar.json"
        fixture.write_text(json.dumps({
            "user_name": "Priya",
            "user_email": "priya@example.com",
            "working_hours": {"start": "09:00", "end": "18:00"},
            "default_view_date": "2026-06-06",
            "calendars": [{
                "id": "calendar-primary",
                "name": "Primary",
                "timezone": "America/New_York",
                "primary": True,
            }],
            "events": [{
                "id": "event-1",
                "calendar_id": "calendar-primary",
                "title": "Platform sync",
                "start": "2026-06-06T10:00:00-04:00",
                "end": "2026-06-06T11:00:00-04:00",
                "timezone": "America/New_York",
                "all_day": False,
                "description": "Weekly platform meeting.",
                "location": "Remote",
                "organizer": {
                    "name": "Priya",
                    "email": "priya@example.com",
                },
                "movable": False,
                "attendees": [{
                    "name": "Marcus",
                    "email": "marcus@example.com",
                    "response_status": "accepted",
                }],
                "status": "confirmed",
                "recurrence_parent_id": "",
                "created_at": "2026-06-01T09:00:00-04:00",
                "updated_at": "2026-06-01T09:00:00-04:00",
            }],
        }))
        self.env_key = fixture_env_var("calendar-external-gaps")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = CalendarService("calendar-external-gaps")
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

    def test_profiles_and_contracts_keep_existing_actions(self):
        self.assertEqual(
            set(CalendarService.CAPABILITY_PROFILES["standard-v1"]),
            set(_BASELINE_CORE) | {"add_event_participants"},
        )
        self.assertEqual(
            set(CalendarService.CAPABILITY_PROFILES["full-v1"]),
            set(_BASELINE_CORE)
            | set(_BASELINE_EXTENSION)
            | {"add_event_participants"},
        )
        for action in _BASELINE_CORE + _BASELINE_EXTENSION:
            self.assertIn(action, CalendarService.ACTION_CONTRACTS)
        contract = CalendarService.ACTION_CONTRACTS["add_event_participants"]
        self.assertTrue(contract.state_effects)
        self.assertIn("state_change", contract.capability_tags)

    def test_add_event_participants_appends_without_replacing(self):
        response = self.client.post("/event/add_participants", json={
            "event_id": "event-1",
            "participants": [
                "dana@example.com",
                "Marcus@example.com",
                "dana@example.com",
                "  ",
            ],
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "updated")
        self.assertEqual(body["added_participants"], ["dana@example.com"])
        attendees = body["event"]["attendees"]
        self.assertEqual(
            [item["email"] for item in attendees],
            ["marcus@example.com", "dana@example.com"],
        )
        self.assertEqual(attendees[0]["response_status"], "accepted")
        self.assertEqual(attendees[1]["response_status"], "needs_action")
        self.assertEqual(
            self.service.events["event-1"]["updated_at"],
            self.service.now_iso(),
        )

    def test_add_event_participants_is_idempotent_and_audited(self):
        first = self.client.post("/event/add_participants", json={
            "event_id": "event-1",
            "participants": ["dana@example.com"],
        })
        self.assertEqual(first.json()["status"], "updated")
        second = self.client.post("/event/add_participants", json={
            "event_id": "event-1",
            "participants": ["dana@example.com"],
        })
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "unchanged")
        self.assertEqual(second.json()["added_participants"], [])
        audit = self.service.get_audit()
        self.assertEqual(
            [entry["action"] for entry in audit],
            ["add_event_participants", "add_event_participants"],
        )
        self.assertTrue(audit[1]["no_op"])

    def test_add_event_participants_errors(self):
        missing = self.client.post("/event/add_participants", json={
            "event_id": "missing",
            "participants": ["dana@example.com"],
        })
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.json()["error"]["code"], "EVENT_NOT_FOUND"
        )
        invalid = self.client.post("/event/add_participants", json={
            "event_id": "event-1",
            "participants": ["", "   "],
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            invalid.json()["error"]["code"], "INVALID_PARTICIPANTS"
        )


if __name__ == "__main__":
    unittest.main()
