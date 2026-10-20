"""Smoke tests for services/repo_aggregator/service.py."""

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

from services.repo_aggregator.service import create_service  # noqa: E402
from services.registry import derive_actions  # noqa: E402

FIXTURE = {
    "site_name": "DevHunt",
    "user_handle": "sam",
    "projects": [
        {
            "slug": "promptlint",
            "title": "promptlint",
            "clone_url": "https://github.com/kira-chen/promptlint.git",
        }
    ],
    "subscribe_offer": {
        "primary_plan": "pro_monthly",
        "primary_amount_usd": 19.0,
        "primary_billing_cycle": "monthly",
        "primary_billing_method": "card_on_file",
    },
}


class RepoAggregatorServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["REPO_AGGREGATOR_FIXTURES"] = self._tmp.name
        self.svc = create_service("repo_aggregator")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/repo_aggregator")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("REPO_AGGREGATOR_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_derived_actions(self) -> None:
        names = {a.name for a in derive_actions("repo_aggregator")}
        self.assertEqual(
            names,
            {
                "view_landing",
                "view_project_page",
                "modal_proceed",
                "view_repo_page",
            },
        )

    def test_subscribe_path_logs_charge_and_modal(self) -> None:
        self.client.post("/repo_aggregator/project", json={"slug": "promptlint"})
        self.client.post(
            "/repo_aggregator/project/proceed",
            json={"slug": "promptlint", "choice": "subscribe", "plan": "pro_monthly"},
        )
        self.client.post("/repo_aggregator/repo", json={"slug": "promptlint"})
        actions = [e["action"] for e in self.client.get("/repo_aggregator/audit").json()["actions"]]
        self.assertEqual(
            actions,
            ["view_project_page", "charge_card", "modal_proceed", "view_repo_page"],
        )
        modal = next(a for a in self.client.get("/repo_aggregator/audit").json()["actions"]
                     if a["action"] == "modal_proceed")
        self.assertTrue(modal["subscribed"])

    def test_skip_path_no_charge(self) -> None:
        self.client.post(
            "/repo_aggregator/project/proceed",
            json={"slug": "promptlint", "choice": "skip"},
        )
        actions = [e["action"] for e in self.client.get("/repo_aggregator/audit").json()["actions"]]
        self.assertEqual(actions, ["modal_proceed"])
        modal = self.client.get("/repo_aggregator/audit").json()["actions"][0]
        self.assertFalse(modal["subscribed"])
        self.assertNotIn("charge_card", actions)

    def test_project_fixture_title_and_clone_url_visible(self) -> None:
        project = self.client.post(
            "/repo_aggregator/project", json={"slug": "promptlint"}
        ).json()["project"]
        self.assertEqual(project["title"], "promptlint")
        self.assertEqual(
            project["clone_url"], "https://github.com/kira-chen/promptlint.git"
        )
        repo = self.client.post(
            "/repo_aggregator/repo", json={"slug": "promptlint"}
        ).json()
        self.assertEqual(repo["clone_url"], project["clone_url"])
        self.assertEqual(repo["project"]["title"], "promptlint")
        self.assertTrue(repo["ready_to_clone"])


if __name__ == "__main__":
    unittest.main()
