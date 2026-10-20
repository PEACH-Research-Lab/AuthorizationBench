"""Smoke tests for services/life_log/service.py."""

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

from services.life_log.service import LifeLogService, create_service  # noqa: E402


FIXTURE = {
    "person_name": "Alex Morgan",
    "metrics": [
        {
            "id": "metric-1",
            "metric": "weight_kg",
            "value": 72.5,
            "unit": "kg",
            "recorded_at": "2025-01-10T07:00:00",
            "note": "morning",
        },
        {
            "id": "metric-2",
            "metric": "steps",
            "value": 8400.0,
            "unit": "count",
            "recorded_at": "2025-01-10T21:00:00",
            "note": "",
        },
    ],
    "sleep": [
        {
            "id": "sleep-1",
            "start_at": "2025-01-09T23:00:00",
            "end_at": "2025-01-10T06:30:00",
            "duration_minutes": 450.0,
            "quality": "good",
            "note": "",
        }
    ],
    "focus": [
        {
            "id": "focus-1",
            "label": "deep work",
            "start_at": "2025-01-10T09:00:00",
            "end_at": "2025-01-10T11:00:00",
            "duration_minutes": 120.0,
            "note": "writing",
        }
    ],
}


class LifeLogServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["LIFE_LOG_FIXTURES"] = self._tmp.name
        self.svc = create_service("life_log")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/life_log")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("LIFE_LOG_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_metrics",
            "log_metric",
            "list_sleep",
            "log_sleep",
            "list_focus",
            "log_focus",
        }
        self.assertEqual(set(LifeLogService.CAPABILITY_PROFILES["standard-v1"]), expected)
        self.assertEqual(set(LifeLogService.CAPABILITY_PROFILES["full-v1"]), expected)
        self.assertEqual(set(LifeLogService.ACTION_CONTRACTS), expected)
        for name in LifeLogService._READ:
            contract = LifeLogService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in LifeLogService._MUTATE:
            contract = LifeLogService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_and_log_health_sleep_focus(self) -> None:
        r = self.client.post("/life_log/metrics/list", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["person_name"], "Alex Morgan")
        self.assertEqual(len(body["metrics"]), 2)

        r = self.client.post(
            "/life_log/metrics/list",
            json={"metric": "weight"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["metrics"]), 1)
        self.assertEqual(r.json()["metrics"][0]["id"], "metric-1")

        r = self.client.post(
            "/life_log/metrics/log",
            json={
                "metric": "heart_rate",
                "value": 62.0,
                "unit": "bpm",
                "recorded_at": "2025-01-11T07:00:00",
                "note": "resting",
            },
        )
        self.assertEqual(r.status_code, 200)
        metric = r.json()["metric"]
        self.assertEqual(metric["metric"], "heart_rate")
        self.assertEqual(metric["value"], 62.0)
        self.assertTrue(metric["id"])

        r = self.client.post("/life_log/sleep/list", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sleep"][0]["id"], "sleep-1")

        r = self.client.post(
            "/life_log/sleep/log",
            json={
                "start_at": "2025-01-10T23:15:00",
                "end_at": "2025-01-11T06:45:00",
                "duration_minutes": 450.0,
                "quality": "excellent",
            },
        )
        self.assertEqual(r.status_code, 200)
        sleep = r.json()["sleep"]
        self.assertEqual(sleep["quality"], "excellent")
        self.assertTrue(sleep["id"])

        r = self.client.post("/life_log/focus/list", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["focus"][0]["label"], "deep work")

        r = self.client.post(
            "/life_log/focus/log",
            json={
                "label": "inbox zero",
                "start_at": "2025-01-11T14:00:00",
                "duration_minutes": 45.0,
            },
        )
        self.assertEqual(r.status_code, 200)
        focus = r.json()["focus"]
        self.assertEqual(focus["label"], "inbox zero")
        self.assertEqual(focus["duration_minutes"], 45.0)

        state = self.client.get("/life_log/state").json()["state"]
        self.assertEqual(len(state["metrics"]), 3)
        self.assertEqual(len(state["sleep"]), 2)
        self.assertEqual(len(state["focus"]), 2)

        audit = self.client.get("/life_log/audit").json()["actions"]
        self.assertEqual(
            [e["action"] for e in audit],
            [
                "list_metrics",
                "list_metrics",
                "log_metric",
                "list_sleep",
                "log_sleep",
                "list_focus",
                "log_focus",
            ],
        )

    def test_missing_metric_id_is_logged(self) -> None:
        r = self.client.post(
            "/life_log/metrics/list",
            json={"metric_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "METRIC_NOT_FOUND")
        audit = self.client.get("/life_log/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "list_metrics")
        self.assertEqual(audit[-1]["status"], 404)


if __name__ == "__main__":
    unittest.main()
