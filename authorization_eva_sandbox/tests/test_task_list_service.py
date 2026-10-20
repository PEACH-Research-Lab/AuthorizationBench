"""Smoke tests for services/task_list/service.py."""

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

from services.task_list.service import TaskListService, create_service  # noqa: E402


FIXTURE = {
    "owner_name": "Alex Morgan",
    "list_name": "Personal",
    "tasks": [
        {
            "id": "task-1",
            "title": "Pay electric bill",
            "notes": "Account ends in 4421",
            "due_at": "2025-01-12T09:00:00",
            "status": "open",
            "priority": "high",
            "created_at": "2025-01-10T08:00:00",
            "completed_at": "",
        },
        {
            "id": "task-2",
            "title": "Buy groceries",
            "notes": "",
            "due_at": "2025-01-11T18:00:00",
            "status": "open",
            "priority": "normal",
            "created_at": "2025-01-10T09:00:00",
            "completed_at": "",
        },
        {
            "id": "task-3",
            "title": "Renew library card",
            "notes": "Done last week",
            "due_at": "2025-01-05T12:00:00",
            "status": "completed",
            "priority": "low",
            "created_at": "2025-01-01T10:00:00",
            "completed_at": "2025-01-06T10:00:00",
        },
    ],
}


class TaskListServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["TASK_LIST_FIXTURES"] = self._tmp.name
        self.svc = create_service("task_list")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/task_list")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("TASK_LIST_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_tasks",
            "get_task",
            "create_task",
            "complete_task",
            "snooze_task",
        }
        self.assertEqual(set(TaskListService.CAPABILITY_PROFILES["standard-v1"]), expected)
        self.assertEqual(set(TaskListService.CAPABILITY_PROFILES["full-v1"]), expected)
        self.assertEqual(set(TaskListService.ACTION_CONTRACTS), expected)
        for name in TaskListService._READ:
            contract = TaskListService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in TaskListService._MUTATE:
            contract = TaskListService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_get_create_complete_snooze(self) -> None:
        r = self.client.post("/task_list/tasks/list", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["list_name"], "Personal")
        self.assertEqual(len(body["tasks"]), 3)
        self.assertEqual(body["tasks"][0]["id"], "task-1")

        r = self.client.post(
            "/task_list/tasks/get",
            json={"task_id": "task-2"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["task"]["title"], "Buy groceries")

        r = self.client.post(
            "/task_list/tasks/create",
            json={
                "title": "Call dentist",
                "notes": "Ask about cleaning",
                "due_at": "2025-01-15T10:00:00",
                "priority": "high",
            },
        )
        self.assertEqual(r.status_code, 200)
        created = r.json()["task"]
        self.assertEqual(created["status"], "open")
        self.assertEqual(created["title"], "Call dentist")
        self.assertTrue(created["id"])

        r = self.client.post(
            "/task_list/tasks/snooze",
            json={"task_id": "task-2", "until": "2025-01-13T18:00:00"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["task"]["due_at"], "2025-01-13T18:00:00")

        r = self.client.post(
            "/task_list/tasks/complete",
            json={"task_id": "task-1"},
        )
        self.assertEqual(r.status_code, 200)
        done = r.json()["task"]
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["completed_at"])

        state = self.client.get("/task_list/state").json()["state"]
        self.assertEqual(len(state["tasks"]), 4)
        self.assertEqual(
            next(t for t in state["tasks"] if t["id"] == "task-1")["status"],
            "completed",
        )
        self.assertEqual(
            next(t for t in state["tasks"] if t["id"] == "task-2")["due_at"],
            "2025-01-13T18:00:00",
        )

        audit = self.client.get("/task_list/audit").json()["actions"]
        self.assertEqual(
            [e["action"] for e in audit],
            ["list_tasks", "get_task", "create_task", "snooze_task", "complete_task"],
        )

    def test_missing_task_is_logged(self) -> None:
        r = self.client.post(
            "/task_list/tasks/get",
            json={"task_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TASK_NOT_FOUND")
        audit = self.client.get("/task_list/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "get_task")
        self.assertEqual(audit[-1]["status"], 404)

    def test_complete_already_done_is_conflict(self) -> None:
        r = self.client.post(
            "/task_list/tasks/complete",
            json={"task_id": "task-3"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "ALREADY_COMPLETED")


if __name__ == "__main__":
    unittest.main()
