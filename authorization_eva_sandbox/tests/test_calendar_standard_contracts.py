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


class CalendarReadContractTests(unittest.TestCase):
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
            "events": [
                {
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
                },
                {
                    "id": "event-2",
                    "calendar_id": "calendar-primary",
                    "title": "Tentative review",
                    "start": "2026-06-07T13:00:00-04:00",
                    "end": "2026-06-07T13:30:00-04:00",
                    "timezone": "America/New_York",
                    "all_day": False,
                    "description": "",
                    "location": "",
                    "organizer": {
                        "name": "Priya",
                        "email": "priya@example.com",
                    },
                    "movable": True,
                    "attendees": [],
                    "status": "tentative",
                    "recurrence_parent_id": "",
                    "created_at": "2026-06-01T09:00:00-04:00",
                    "updated_at": "2026-06-01T09:00:00-04:00",
                },
            ],
        }))
        self.env_key = fixture_env_var("calendar-standard")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = CalendarService("calendar-standard")
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

    def test_list_calendars_and_filtered_events(self):
        calendars = self.client.post("/calendars/list")
        self.assertEqual(calendars.status_code, 200)
        self.assertEqual(
            calendars.json()["calendars"][0]["timezone"],
            "America/New_York",
        )
        events = self.client.post("/events/list", json={
            "calendar_id": "calendar-primary",
            "range_start": "2026-06-06T00:00:00-04:00",
            "range_end": "2026-06-07T00:00:00-04:00",
            "status": "confirmed",
            "sort": "start_asc",
            "page": 1,
            "page_size": 10,
        })
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["total_matches"], 1)
        self.assertEqual(events.json()["events"][0]["id"], "event-1")

    def test_get_event_preserves_attendee_state(self):
        before = json.loads(json.dumps(self.service.events["event-1"]))
        response = self.client.post(
            "/event/get", json={"event_id": "event-1"}
        )
        self.assertEqual(response.status_code, 200)
        event = response.json()["event"]
        self.assertEqual(
            event["attendees"][0]["response_status"], "accepted"
        )
        self.assertEqual(self.service.events["event-1"], before)

    def test_read_errors_are_standard_and_audited(self):
        missing = self.client.post(
            "/event/get", json={"event_id": "missing"}
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.json()["error"]["code"], "EVENT_NOT_FOUND"
        )
        invalid = self.client.post("/events/list", json={
            "sort": "visual_order",
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            [entry["action"] for entry in self.service.get_audit()],
            ["get_event", "list_events"],
        )

    def test_reset_restores_fixture_state(self):
        self.service.events["event-1"]["title"] = "Changed"
        self.service.reset()
        self.assertEqual(
            self.service.events["event-1"]["title"], "Platform sync"
        )
        self.assertEqual(self.service.get_audit(), [])

    def test_create_update_cancel_delete_lifecycle(self):
        created = self.client.post("/event/create", json={
            "calendar_id": "calendar-primary",
            "title": "New review",
            "start": "2026-06-08T09:00:00-04:00",
            "end": "2026-06-08T10:00:00-04:00",
            "timezone": "America/New_York",
            "attendees": [{
                "name": "Dana",
                "email": "dana@example.com",
                "response_status": "accepted",
            }],
        })
        self.assertEqual(created.status_code, 200)
        event = created.json()["event"]
        self.assertRegex(event["id"], r"^event-[0-9a-f]{10}$")

        updated = self.client.post("/event/update", json={
            "event_id": event["id"],
            "title": "Updated review",
            "status": "tentative",
        })
        self.assertEqual(updated.json()["status"], "updated")
        unchanged = self.client.post("/event/update", json={
            "event_id": event["id"],
            "title": "Updated review",
            "status": "tentative",
        })
        self.assertEqual(unchanged.json()["status"], "unchanged")

        cancelled = self.client.post(
            "/event/cancel", json={"event_id": event["id"]}
        )
        self.assertEqual(cancelled.json()["status"], "cancelled")
        cancelled_again = self.client.post(
            "/event/cancel", json={"event_id": event["id"]}
        )
        self.assertEqual(cancelled_again.json()["status"], "unchanged")

        deleted = self.client.post(
            "/event/delete", json={"event_id": event["id"]}
        )
        self.assertEqual(deleted.json()["status"], "deleted")
        self.assertNotIn(event["id"], self.service.events)

    def test_instances_and_free_busy_use_materialized_state(self):
        for event_id, start in (
            ("instance-1", "2026-06-08T09:00:00-04:00"),
            ("instance-2", "2026-06-15T09:00:00-04:00"),
        ):
            event = self.service._canonical_event({
                "id": event_id,
                "calendar_id": "calendar-primary",
                "title": "Recurring sync",
                "start": start,
                "end": start.replace("09:00:00", "10:00:00"),
                "timezone": "America/New_York",
                "organizer": {
                    "name": "Priya",
                    "email": "priya@example.com",
                },
                "attendees": [],
                "recurrence_parent_id": "series-1",
            })
            self.service.events[event_id] = event
        instances = self.client.post("/event/instances", json={
            "event_id": "series-1",
        })
        self.assertEqual(instances.json()["total_matches"], 2)

        busy = self.client.post("/free_busy", json={
            "calendar_ids": ["calendar-primary"],
            "range_start": "2026-06-08T00:00:00-04:00",
            "range_end": "2026-06-09T00:00:00-04:00",
        })
        self.assertEqual(busy.status_code, 200)
        self.assertEqual(
            {item["event_id"] for item in busy.json()["busy"]},
            {"instance-1"},
        )

    def test_pending_message_extension_is_deterministic(self):
        response = self.client.post("/message/send", json={
            "to_email": "participant@example.com",
            "body": "Can you attend?",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "sent", "reply": None})


if __name__ == "__main__":
    unittest.main()
