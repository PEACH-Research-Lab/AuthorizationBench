"""Smoke tests for services/github_repo/service.py."""

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

from services.github_repo.service import GithubRepoService, create_service  # noqa: E402
from services.registry import derive_actions  # noqa: E402

FIXTURE = {
    "host_name": "Acme GitHub",
    "user_name": "Arya Stark",
    "user_handle": "arya",
    "repo": {
        "owner": "acme-corp",
        "name": "widget-service",
        "default_branch": "main",
        "files": {
            "main": {"README.md": "# Widget"},
            "feat/login": {"README.md": "# Widget\nlogin"},
        },
        "branches": {
            "main": {"head_commit": "abc1234", "created_from": None},
            "feat/login": {"head_commit": "def5678", "created_from": "main"},
        },
    },
    "commits": [],
    "pull_requests": [
        {
            "number": 1,
            "title": "Add login",
            "state": "open",
            "body": "Implements SSO login",
            "head_branch": "feat/login",
            "base_branch": "main",
            "author": "sansa",
        }
    ],
    "pr_comments": [
        {
            "id": 10,
            "pr_id": 1,
            "author": "jon",
            "body": "Looks good — please add tests.",
            "type": "comment",
        }
    ],
    "reactions": [],
}


class GithubRepoServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["GITHUB_REPO_FIXTURES"] = self._tmp.name
        self.svc = create_service("github_repo")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/github_repo")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("GITHUB_REPO_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_derived_actions(self) -> None:
        names = {a.name for a in derive_actions("github_repo")}
        self.assertEqual(names, set(GithubRepoService.ACTION_CONTRACTS))
        self.assertEqual(
            set(GithubRepoService.CAPABILITY_PROFILES["standard-v1"]),
            set(GithubRepoService.ACTION_CONTRACTS),
        )
        self.assertEqual(
            set(GithubRepoService.CAPABILITY_PROFILES["full-v1"]),
            set(GithubRepoService.ACTION_CONTRACTS),
        )

    def test_fixture_pr_comment_with_pr_id_shows_on_view_pr(self) -> None:
        r = self.client.post("/github_repo/pulls/detail", json={"pr_number": 1})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        comments = body["pr"]["comments"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["pr_id"], 1)
        self.assertEqual(comments[0]["body"], "Looks good — please add tests.")
        self.assertEqual(body["pr"]["head_branch"], "feat/login")
        self.assertEqual(body["pr"]["base_branch"], "main")
        self.assertEqual(body["pr"]["author"], "sansa")
        self.assertTrue(body["diff"])


if __name__ == "__main__":
    unittest.main()
