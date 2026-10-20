"""Smoke tests for services/zoom/service.py."""

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

from services.zoom.service import create_service  # noqa: E402
from services.registry import derive_actions  # noqa: E402

MEETING = {
    "meeting": {
        "meeting_id": "744-118-203",
        "topic": "Sync",
        "host": "Maya",
        "scheduled_start": "2026-06-01T09:00:00Z",
        "scheduled_end": "2026-06-01T10:00:00Z",
        "invitation_status": "pending",
        "local_participant_status": "not_joined",
        "conference_status": "scheduled",
        "recording_status": "off",
        "transcription_status": "off",
        "participants": [],
        "transcript": {
            "transcript_id": "transcript-1",
            "state": "off",
            "language": "en",
            "segments": [],
        },
        "summary": {
            "summary_id": "summary-1",
            "state": "ready",
            "coverage_start": "",
            "coverage_end": "",
            "title": "Summary",
            "content": "Notes",
            "created_at": "",
            "updated_at": "",
            "default_recipients": ["team@example.com"],
        },
    }
}

SETTINGS = {
    "settings": {
        "auto_join_meetings": False,
        "auto_record": True,
        "auto_transcribe": False,
        "auto_share_summary": "all_participants",
    }
}


class ZoomServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        combined = {**MEETING, **SETTINGS}
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(combined, self._tmp)
        self._tmp.close()
        os.environ["ZOOM_FIXTURES"] = self._tmp.name
        self.svc = create_service("zoom")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/zoom")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("ZOOM_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_derived_actions(self) -> None:
        names = {a.name for a in derive_actions("zoom")}
        self.assertIn("get_meeting", names)
        self.assertIn("accept_invitation", names)
        self.assertIn("get_transcript", names)
        self.assertIn("view_desktop", names)
        self.assertIn("join_meeting", names)
        self.assertIn("open_summary_in_email", names)
        self.assertNotIn("ringing_timed_out", names)

    def test_accept_and_environment_start_are_separate(self) -> None:
        self.client.post("/zoom/invitation/accept")
        self.svc.inject({"action": "conference_start"})
        meeting = self.client.post("/zoom/meeting/get").json()["meeting"]
        self.assertEqual(meeting["conference_status"], "in_progress")
        self.assertEqual(meeting["local_participant_status"], "joined")

    def test_environment_end_is_not_agent_action(self) -> None:
        self.svc.inject({"action": "conference_end"})
        self.assertEqual(self.svc.conference_status, "ended")
        actions = [a["action"] for a in self.client.get("/zoom/audit").json()["actions"]]
        self.assertEqual(actions, ["conference_end"])

    def test_summary_returns_canonical_artifact(self) -> None:
        r = self.client.post("/zoom/summary/get")
        self.assertEqual(
            r.json()["summary"]["default_recipients"],
            ["team@example.com"],
        )
        log = self.client.get("/zoom/audit").json()["actions"][0]
        self.assertEqual(log["action"], "get_summary")


if __name__ == "__main__":
    unittest.main()
