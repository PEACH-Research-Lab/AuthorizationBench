"""Smoke tests for services/issue_tracker/service.py."""

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

from services.issue_tracker.service import IssueTrackerService, create_service  # noqa: E402


FIXTURE = {
    "workspace_name": "Acme",
    "team_key": "ENG",
    "team_name": "Engineering",
    "current_user": "arya",
    "issues": [
        {
            "id": "issue-1",
            "title": "Fix login redirect",
            "description": "Users land on blank page after SSO",
            "status": "in_progress",
            "assignee": "arya",
            "priority": "high",
            "created_at": "2025-01-10T08:00:00",
            "updated_at": "2025-01-11T09:00:00",
        },
        {
            "id": "issue-2",
            "title": "Add export CSV",
            "description": "",
            "status": "todo",
            "assignee": "sansa",
            "priority": "medium",
            "created_at": "2025-01-10T09:00:00",
            "updated_at": "2025-01-10T09:00:00",
        },
        {
            "id": "issue-3",
            "title": "Retire legacy webhook",
            "description": "Done last sprint",
            "status": "done",
            "assignee": "jon",
            "priority": "low",
            "created_at": "2025-01-01T10:00:00",
            "updated_at": "2025-01-06T10:00:00",
        },
    ],
}


class IssueTrackerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["ISSUE_TRACKER_FIXTURES"] = self._tmp.name
        self.svc = create_service("issue_tracker")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/issue_tracker")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("ISSUE_TRACKER_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_issues",
            "get_issue",
            "create_issue",
            "update_issue",
        }
        self.assertEqual(
            set(IssueTrackerService.CAPABILITY_PROFILES["standard-v1"]), expected
        )
        self.assertEqual(
            set(IssueTrackerService.CAPABILITY_PROFILES["full-v1"]), expected
        )
        self.assertEqual(set(IssueTrackerService.ACTION_CONTRACTS), expected)
        for name in IssueTrackerService._READ:
            contract = IssueTrackerService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in IssueTrackerService._MUTATE:
            contract = IssueTrackerService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_get_create_update(self) -> None:
        r = self.client.post("/issue_tracker/issues/list", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["team_key"], "ENG")
        self.assertEqual(len(body["issues"]), 3)
        self.assertEqual(body["issues"][0]["id"], "issue-1")

        r = self.client.post(
            "/issue_tracker/issues/get",
            json={"issue_id": "issue-2"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["issue"]["title"], "Add export CSV")

        r = self.client.post(
            "/issue_tracker/issues/create",
            json={
                "title": "Document onboarding",
                "description": "Write runbook",
                "status": "backlog",
                "assignee": "arya",
                "priority": "low",
            },
        )
        self.assertEqual(r.status_code, 200)
        created = r.json()["issue"]
        self.assertEqual(created["status"], "backlog")
        self.assertEqual(created["title"], "Document onboarding")
        self.assertTrue(created["id"])

        r = self.client.post(
            "/issue_tracker/issues/update",
            json={"issue_id": "issue-2", "status": "in_progress"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["issue"]["status"], "in_progress")
        self.assertEqual(r.json()["issue"]["assignee"], "sansa")

        r = self.client.post(
            "/issue_tracker/issues/update",
            json={"issue_id": "issue-1", "assignee": "jon"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["issue"]["assignee"], "jon")
        self.assertEqual(r.json()["issue"]["status"], "in_progress")

        state = self.client.get("/issue_tracker/state").json()["state"]
        self.assertEqual(len(state["issues"]), 4)
        self.assertEqual(
            next(i for i in state["issues"] if i["id"] == "issue-2")["status"],
            "in_progress",
        )
        self.assertEqual(
            next(i for i in state["issues"] if i["id"] == "issue-1")["assignee"],
            "jon",
        )

        audit = self.client.get("/issue_tracker/audit").json()["actions"]
        self.assertEqual(
            [e["action"] for e in audit],
            [
                "list_issues",
                "get_issue",
                "create_issue",
                "update_issue",
                "update_issue",
            ],
        )

    def test_missing_issue_is_logged(self) -> None:
        r = self.client.post(
            "/issue_tracker/issues/get",
            json={"issue_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "ISSUE_NOT_FOUND")
        audit = self.client.get("/issue_tracker/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "get_issue")
        self.assertEqual(audit[-1]["status"], 404)

    def test_update_missing_issue_is_logged(self) -> None:
        r = self.client.post(
            "/issue_tracker/issues/update",
            json={"issue_id": "missing", "status": "done"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "ISSUE_NOT_FOUND")
        audit = self.client.get("/issue_tracker/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "update_issue")
        self.assertEqual(audit[-1]["status"], 404)


if __name__ == "__main__":
    unittest.main()
