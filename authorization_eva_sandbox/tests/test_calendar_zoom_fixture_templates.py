"""Calendar and Zoom FixtureTemplate contracts and runtime smoke tests."""
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
from authorization_eva_sandbox.services.calendar.service import CalendarService
from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.services.zoom.service import ZoomService


class CalendarZoomTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registry = ServiceRegistry.load()
        cls.calendar = registry.templates["calendar"].fixture_template
        cls.zoom = registry.templates["zoom"].fixture_template

    def test_services_use_only_canonical_template_declaration(self):
        for service_class, template in (
            (CalendarService, self.calendar),
            (ZoomService, self.zoom),
        ):
            self.assertTrue(hasattr(service_class, "FIXTURE_TEMPLATE"))
            self.assertFalse(hasattr(service_class, "FIXTURE_SCHEMA"))
            self.assertFalse(hasattr(service_class, "FIXTURE_SLOTS"))
            self.assertTrue(template.migrated)

    def test_every_field_has_source_meaning_and_format(self):
        for template in (self.calendar, self.zoom):
            for field in template.fields:
                self.assertTrue(field.source, field.path)
                self.assertTrue(field.meaning, field.path)
                self.assertTrue(field.format, field.path)

    def test_s0010_material_kinds_have_unique_template_slots(self):
        registry = ServiceRegistry.load()
        calendar_slots = registry.compatible_fixture_slots(
            "calendar", "calendar_event"
        )
        zoom_slots = registry.compatible_fixture_slots(
            "zoom", "meeting_record"
        )
        self.assertEqual(
            [slot.slot_id for slot in calendar_slots], ["calendar_event"]
        )
        self.assertEqual(
            [slot.slot_id for slot in zoom_slots], ["meeting_record"]
        )

    def test_templates_are_generic_not_s0010_specific(self):
        for template in (self.calendar, self.zoom):
            serialized = json.dumps(template.to_dict()).lower()
            for seed_specific in (
                "s0010", "private gossip", "atlas weekly sync",
            ):
                self.assertNotIn(seed_specific, serialized)

    def test_calendar_windows_are_typed_and_statuses_are_enums(self):
        schema = self.calendar.schema
        participant = schema["pending_meeting"]["participants"][0]
        self.assertEqual(
            participant["availability_windows"],
            [{"start": "str", "end": "str"}],
        )
        self.assertTrue(
            schema["pending_requests"][0]["status"].startswith("enum:")
        )
        self.assertTrue(schema["pending_meeting"]["status"].startswith("enum:"))

    def test_zoom_transcript_has_canonical_segments_and_full_settings(self):
        schema = self.zoom.schema
        segment = schema["meeting"]["transcript"]["segments"][0]
        self.assertEqual(segment["segment_id"], "str")
        self.assertEqual(segment["start_time"], "str")
        self.assertEqual(segment["end_time"], "str")
        self.assertEqual(segment["participant_id"], "str")
        self.assertIn("auto_share_screen", schema["settings"])
        self.assertIn("join_audio_on_start", schema["settings"])
        self.assertNotIn("compose_url", schema["meeting"]["summary"])
        self.assertNotIn("adjourned", schema["meeting"])

    def test_noncanonical_zoom_summary_setting_is_rejected(self):
        issues = validate_fixture(
            {"settings": {"auto_share_summary": "team@example.com"}},
            self.zoom.schema,
        )
        self.assertTrue(any("auto_share_summary" in issue for issue in issues))


class CalendarZoomTemplateRuntimeTests(unittest.TestCase):
    def _client(self, template: str, fixture: dict):
        temp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(fixture, temp)
        temp.close()
        service_id = f"{template}_template_pilot"
        os.environ[fixture_env_var(service_id)] = temp.name
        if template == "calendar":
            service = CalendarService(service_id)
        else:
            service = ZoomService(service_id)
        service.load_data()
        app = FastAPI()
        app.include_router(service.router, prefix=f"/{template}")
        return service, TestClient(app), Path(temp.name), service_id

    def test_calendar_event_template_supports_get_and_create(self):
        service, client, temp, service_id = self._client("calendar", {
            "events": [{
                "id": "event-1",
                "title": "Planning",
                "start": "2026-06-01T09:00:00Z",
                "end": "2026-06-01T10:00:00Z",
                "description": "Planning meeting",
                "location": "Remote",
                "movable": True,
                "attendees": [{
                    "name": "Priya",
                    "email": "priya@example.com",
                    "response_status": "accepted",
                }],
            }],
        })
        try:
            fetched = client.post(
                "/calendar/event/get", json={"event_id": "event-1"}
            ).json()
            self.assertEqual(fetched["event"]["title"], "Planning")
            created = client.post("/calendar/event/create", json={
                "title": "Follow-up",
                "start": "2026-06-02T09:00",
                "end": "2026-06-02T09:30",
                "attendees": [{
                    "name": "Priya",
                    "email": "priya@example.com",
                    "response_status": "accepted",
                }],
            }).json()
            self.assertIn(created["event"]["id"], service.events)
        finally:
            os.environ.pop(fixture_env_var(service_id), None)
            temp.unlink(missing_ok=True)

    def test_zoom_template_exposes_timestamped_transcript(self):
        service, client, temp, service_id = self._client("zoom", {
            "meeting": {
                "meeting_id": "meeting-1",
                "topic": "Planning",
                "host": "Priya",
                "scheduled_start": "2026-06-01T09:00:00Z",
                "scheduled_end": "2026-06-01T10:00:00Z",
                "invitation_status": "accepted",
                "local_participant_status": "left",
                "conference_status": "ended",
                "recording_status": "ready",
                "transcription_status": "ready",
                "participants": [],
                "transcript": {
                    "transcript_id": "transcript-1",
                    "state": "ready",
                    "language": "en",
                    "segments": [{
                        "segment_id": "segment-1",
                        "participant_id": "",
                        "start_time": "2026-06-01T09:30:00Z",
                        "end_time": "2026-06-01T09:30:30Z",
                        "text": "The meeting is complete.",
                        "language": "en",
                        "final": True,
                    }],
                },
                "summary": {
                    "summary_id": "summary-1",
                    "state": "off",
                    "coverage_start": "",
                    "coverage_end": "",
                    "title": "",
                    "content": "",
                    "created_at": "",
                    "updated_at": "",
                    "default_recipients": [],
                },
            },
        })
        try:
            transcript = client.post(
                "/zoom/transcript/get", json={}
            ).json()
            self.assertEqual(
                transcript["segments"][0]["start_time"],
                "2026-06-01T09:30:00Z",
            )
            self.assertEqual(service.conference_status, "ended")
        finally:
            os.environ.pop(fixture_env_var(service_id), None)
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
