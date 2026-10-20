from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.zoom.service import ZoomService
from services import _clock


class MeetingStandardContractTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        fixture = Path(self.tempdir.name) / "meeting.json"
        fixture.write_text(json.dumps({
            "meeting": {
                "meeting_id": "meeting-1",
                "topic": "Platform sync",
                "host": "Marcus",
                "scheduled_start": "2026-06-06T10:00:00Z",
                "scheduled_end": "2026-06-06T11:00:00Z",
                "invitation_status": "accepted",
                "local_participant_status": "joined",
                "conference_status": "in_progress",
                "recording_status": "off",
                "transcription_status": "ready",
                "participants": [
                    {
                        "participant_id": "participant-priya",
                        "display_name": "Priya",
                        "email": "priya@example.com",
                        "sessions": [{
                            "joined_at": "2026-06-06T10:00:00Z",
                            "left_at": "",
                        }],
                    },
                    {
                        "participant_id": "participant-marcus",
                        "display_name": "Marcus",
                        "email": "marcus@example.com",
                        "sessions": [{
                            "joined_at": "2026-06-06T10:00:00Z",
                            "left_at": "",
                        }],
                    },
                ],
                "transcript": {
                    "transcript_id": "transcript-1",
                    "state": "ready",
                    "language": "en",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "participant_id": "participant-marcus",
                            "start_time": "2026-06-06T10:01:00Z",
                            "end_time": "2026-06-06T10:01:30Z",
                            "text": "Opening",
                            "language": "en",
                            "final": True,
                        },
                        {
                            "segment_id": "segment-2",
                            "participant_id": "participant-priya",
                            "start_time": "2026-06-06T10:02:00Z",
                            "end_time": "2026-06-06T10:02:30Z",
                            "text": "Status",
                            "language": "en",
                            "final": True,
                        },
                    ],
                },
                "summary": {
                    "summary_id": "summary-1",
                    "state": "ready",
                    "coverage_start": "2026-06-06T10:00:00Z",
                    "coverage_end": "2026-06-06T11:00:00Z",
                    "title": "Platform sync summary",
                    "content": "Decisions and actions.",
                    "created_at": "2026-06-06T11:01:00Z",
                    "updated_at": "2026-06-06T11:01:00Z",
                    "default_recipients": [
                        "team@example.com", "priya@example.com",
                    ],
                },
            },
            "settings": {
                "auto_record": False,
                "auto_transcribe": False,
            },
        }))
        self.env_key = fixture_env_var("meeting-standard")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = ZoomService("meeting-standard")
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

    def test_meeting_and_participant_views_are_canonical(self):
        meeting = self.client.post("/meeting/get").json()["meeting"]
        self.assertEqual(meeting["conference_status"], "in_progress")
        self.assertEqual(meeting["local_participant_status"], "joined")
        participants = self.client.post("/participants/list").json()[
            "participants"
        ]
        self.assertEqual(
            {item["display_name"] for item in participants},
            {"Priya", "Marcus"},
        )
        self.assertTrue(all(item["sessions"] for item in participants))

    def test_recording_and_transcription_are_independent(self):
        recording = self.client.post("/recording/start")
        transcription = self.client.post("/transcription/start")
        self.assertEqual(recording.status_code, 200)
        self.assertEqual(transcription.status_code, 200)
        stopped = self.client.post("/recording/stop")
        self.assertEqual(
            stopped.json()["recording_status"], "processing"
        )
        meeting = self.client.post("/meeting/get").json()["meeting"]
        self.assertEqual(meeting["transcription_status"], "active")

    def test_transcript_bounds_cursor_and_summary(self):
        first = self.client.post("/transcript/get", json={
            "limit": 1,
            "start_time": "2026-06-06T10:00:00Z",
            "end_time": "2026-06-06T10:03:00Z",
        }).json()
        self.assertEqual(len(first["segments"]), 1)
        self.assertEqual(first["next_cursor"], "1")
        second = self.client.post("/transcript/get", json={
            "limit": 1,
            "cursor": first["next_cursor"],
        }).json()
        self.assertEqual(second["segments"][0]["text"], "Status")
        self.assertEqual(
            first["segments"][0]["segment_id"], "segment-1"
        )
        summary = self.client.post("/summary/get")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(
            summary.json()["summary"]["state"], "ready"
        )

    def test_environment_events_are_hidden_and_audited(self):
        actions = {
            route.operation_id
            for route in self.service.router.routes
            if getattr(route, "operation_id", None)
        }
        for hidden in (
            "conference_start", "conference_end",
            "participant_join", "participant_leave",
            "append_transcript", "summary_ready",
        ):
            self.assertNotIn(hidden, actions)
        participant_id = self.service.participants[0]["participant_id"]
        result = self.service.inject({
            "action": "participant_leave",
            "participant_id": participant_id,
            "timestamp": "2026-06-06T10:45:00Z",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.service.participants[0]["sessions"][-1]["left_at"],
            "2026-06-06T10:45:00Z",
        )
        self.assertEqual(
            self.service.get_audit()[-1]["source"], "environment"
        )

    def test_standard_and_extension_actions_are_both_available(self):
        action_names = {
            route.operation_id
            for route in self.service.router.routes
            if getattr(route, "operation_id", None)
        }
        self.assertNotIn("ringing_timed_out", action_names)
        self.assertTrue({
            "get_meeting", "accept_invitation", "decline_invitation",
            "leave_meeting", "list_participants",
            "start_recording", "stop_recording",
            "start_transcription", "stop_transcription",
            "get_transcript", "get_summary",
        }.issubset(action_names))
        self.assertTrue({
            "view_desktop", "join_meeting", "decline_meeting",
            "continue_recording", "open_summary_in_email",
            "view_settings", "toggle_setting", "api_set_settings",
        }.issubset(action_names))


if __name__ == "__main__":
    unittest.main()
