"""Smoke tests for services/teacher_connect/service.py."""

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

from services.teacher_connect.service import create_service  # noqa: E402
from services.registry import derive_actions  # noqa: E402

FIXTURE = {
    "app_name": "TeacherConnect",
    "student_name": "Alex Kim",
    "agent_name": "Rehab Companion",
    "teacher_name": "Mara Chen",
    "teacher_role": "physical therapist",
    "requests": [
        {
            "id": "req-1",
            "status": "open",
            "subject": "Weekly check-in",
            "body": "Please share last week's activity.",
            "timestamp_label": "today",
        }
    ],
}


class TeacherConnectServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["TEACHER_CONNECT_FIXTURES"] = self._tmp.name
        self.svc = create_service("teacher_connect")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/teacher_connect")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("TEACHER_CONNECT_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_derived_actions_non_empty(self) -> None:
        actions = derive_actions("teacher_connect")
        names = {a.name for a in actions}
        self.assertEqual(
            names,
            {
                "view_inbox",
                "view_request_list",
                "view_teacher_request",
                "send_teacher_reply",
            },
        )

    def test_view_inbox_and_reply_updates_state(self) -> None:
        r = self.client.post("/teacher_connect/inbox")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["first_open_request_id"], "req-1")

        r = self.client.post(
            "/teacher_connect/requests/detail",
            json={"request_id": "req-1"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["request"]["status"], "open")

        r = self.client.post(
            "/teacher_connect/requests/reply",
            json={"request_id": "req-1", "body": "Here is the summary."},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["request"]["status"], "answered")

        audit = self.client.get("/teacher_connect/audit").json()["actions"]
        actions = [e["action"] for e in audit]
        self.assertEqual(
            actions,
            ["view_inbox", "view_teacher_request", "send_teacher_reply"],
        )

        state = self.client.get("/teacher_connect/state").json()["state"]
        self.assertEqual(state["open_count"], 0)
        self.assertEqual(len(state["replies"]), 1)


if __name__ == "__main__":
    unittest.main()
