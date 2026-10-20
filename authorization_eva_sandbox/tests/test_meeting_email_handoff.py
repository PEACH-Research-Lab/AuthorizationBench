from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.multi_server import create_app


class MeetingEmailHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        meeting_fixture = root / "meeting.json"
        meeting_fixture.write_text(json.dumps({
            "meeting": {
                "meeting_id": "meeting-1",
                "topic": "Platform sync",
                "host": "Marcus",
                "scheduled_start": "2026-06-06T10:00:00Z",
                "scheduled_end": "2026-06-06T11:00:00Z",
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
                    "segments": [],
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
        }))
        email_fixture = root / "email.json"
        email_fixture.write_text(json.dumps({
            "user_name": "Priya",
            "user_email": "priya@example.com",
            "emails": [],
        }))
        self.previous = {}
        for service_id, fixture in (
            ("meeting", meeting_fixture),
            ("mail", email_fixture),
        ):
            key = fixture_env_var(service_id)
            self.previous[key] = os.environ.get(key)
            os.environ[key] = str(fixture)
        app = create_app(
            [
                {"id": "meeting", "template": "zoom"},
                {"id": "mail", "template": "email_client"},
            ],
            reference_time="2026-06-06T10:11:00Z",
            grader_token="grader",
        )
        self.client = TestClient(app)
        self.services = app.state.services

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def test_handoff_creates_real_draft_then_send_draft_moves_it(self):
        handoff = self.client.post("/meeting/summary/email_draft")
        self.assertEqual(handoff.status_code, 200)
        payload = handoff.json()
        draft_id = payload["email_draft_id"]
        self.assertEqual(
            payload["recipients"],
            ["team@example.com", "priya@example.com"],
        )
        email = self.services["mail"]
        self.assertEqual(email.emails[draft_id]["folder"], "drafts")
        self.assertEqual(
            email.emails[draft_id]["body"], "Decisions and actions."
        )
        email_audit = email.get_audit()[-1]
        self.assertEqual(email_audit["action"], "create_draft")
        self.assertEqual(email_audit["source"], "service:zoom")
        meeting_audit = self.services["meeting"].get_audit()[-1]
        self.assertEqual(
            meeting_audit["effect"]["email_draft_id"], draft_id
        )

        sent = self.client.post(
            "/mail/drafts/send", json={"draft_id": draft_id}
        )
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(email.emails[draft_id]["folder"], "sent")

    def test_missing_email_service_creates_no_partial_state(self):
        meeting = self.services["meeting"]
        meeting.bind_service_resolver(lambda _reference: None)
        before = dict(self.services["mail"].emails)
        response = self.client.post("/meeting/summary/email_draft")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "EMAIL_SERVICE_UNAVAILABLE",
        )
        self.assertEqual(self.services["mail"].emails, before)


if __name__ == "__main__":
    unittest.main()
