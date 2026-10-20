"""Docs portal comment gaps borrowed from SmolClaw Docs."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.docs_portal.service import DocsPortalService
from authorization_eva_sandbox.services.registry import ServiceRegistry
from services import _clock


EXISTING_ACTIONS = frozenset({
    "list_portal_folder",
    "view_portal_file",
})

NEW_ACTIONS = frozenset({
    "list_comments",
    "add_comment",
})


class DocsPortalCommentsTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name) / "workspace"
        root.mkdir()
        (root / "notes.md").write_text("hello notes\n", encoding="utf-8")
        (root / "readme.md").write_text("readme body\n", encoding="utf-8")

        fixture = Path(self.tempdir.name) / "docs.json"
        fixture.write_text(json.dumps({
            "workspace_root": str(root),
            "portal_title": "Acme Docs",
            "user_name": "Priya",
            "comments": [
                {
                    "id": "c-seed",
                    "path": "notes.md",
                    "author": "Sam",
                    "body": "Please expand the intro.",
                    "created_at": "2026-01-01T09:00:00Z",
                },
            ],
        }))
        self.env_key = fixture_env_var("docs-portal-comments")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = DocsPortalService("docs-portal-comments")
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

    def test_existing_actions_remain_registered(self):
        spec = ServiceRegistry.load(derive_live=True).templates["docs_portal"]
        live = {action.name for action in spec.actions}
        self.assertTrue(EXISTING_ACTIONS.issubset(live))
        self.assertTrue(NEW_ACTIONS.issubset(live))

        standard = set(spec.capability_profiles["standard-v1"])
        full = set(spec.capability_profiles["full-v1"])
        self.assertTrue(EXISTING_ACTIONS.issubset(standard))
        self.assertTrue(NEW_ACTIONS.issubset(standard))
        self.assertTrue(EXISTING_ACTIONS.issubset(full))
        self.assertTrue(NEW_ACTIONS.issubset(full))

        for name in EXISTING_ACTIONS | NEW_ACTIONS:
            self.assertIn(name, DocsPortalService.ACTION_CONTRACTS)

    def test_nothing_removed_from_profiles(self):
        for profile in ("standard-v1", "full-v1"):
            actions = DocsPortalService.CAPABILITY_PROFILES[profile]
            self.assertEqual(set(actions), EXISTING_ACTIONS | NEW_ACTIONS)
            self.assertEqual(len(actions), len(set(actions)))

    def test_mutation_contracts_declare_state_change(self):
        mutate = DocsPortalService.ACTION_CONTRACTS["add_comment"]
        self.assertTrue(mutate.state_effects)
        self.assertIn("state_change", mutate.capability_tags)
        for name in ("list_portal_folder", "view_portal_file", "list_comments"):
            contract = DocsPortalService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertIn("read", contract.capability_tags)
            self.assertNotIn("state_change", contract.capability_tags)

    def test_list_comments_is_read_only_and_audited(self):
        before = list(self.service.get_state()["comments"])
        response = self.client.post("/comments/list", json={"path": "notes.md"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["comment_count"], 1)
        self.assertEqual(body["comments"][0]["id"], "c-seed")
        self.assertEqual(body["comments"][0]["author"], "Sam")
        self.assertEqual(self.service.get_state()["comments"], before)
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "list_comments")
        self.assertEqual(audit["comment_count"], 1)
        self.assertEqual(audit["path"], "notes.md")

    def test_add_comment_appends_deterministically(self):
        response = self.client.post("/comments/add", json={
            "path": "notes.md",
            "body": "Looks good to ship.",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        comment = body["comment"]
        self.assertEqual(comment["path"], "notes.md")
        self.assertEqual(comment["author"], "Priya")
        self.assertEqual(comment["body"], "Looks good to ship.")
        self.assertEqual(comment["created_at"], self.service.now_iso())
        self.assertTrue(str(comment["id"]).startswith("comment-"))

        listed = self.client.post("/comments/list", json={"path": "notes.md"})
        self.assertEqual(listed.status_code, 200)
        ids = [c["id"] for c in listed.json()["comments"]]
        self.assertEqual(ids, ["c-seed", comment["id"]])

        state = self.service.get_state()
        self.assertEqual(state["comment_count"], 2)
        audit = self.service.get_audit()
        self.assertEqual(audit[-2]["action"], "add_comment")
        self.assertEqual(audit[-2]["comment_id"], comment["id"])

    def test_add_comment_errors_are_audited(self):
        missing = self.client.post("/comments/add", json={
            "path": "missing.md",
            "body": "orphan",
        })
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "DOCUMENT_NOT_FOUND")

        empty = self.client.post("/comments/add", json={
            "path": "notes.md",
            "body": "   ",
        })
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(empty.json()["error"]["code"], "INVALID_BODY")

        for entry in self.service.get_audit()[-2:]:
            self.assertEqual(entry["action"], "add_comment")
            self.assertIn(entry["status"], (404, 422))

    def test_list_comments_missing_document_is_audited(self):
        response = self.client.post("/comments/list", json={"path": "gone.md"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "DOCUMENT_NOT_FOUND")
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "list_comments")
        self.assertEqual(audit["status"], 404)

    def test_existing_reads_still_work(self):
        listed = self.client.post("/list", json={"path": ""})
        self.assertEqual(listed.status_code, 200)
        body = listed.json()
        self.assertEqual(body["portal_title"], "Acme Docs")
        names = {e["name"] for e in body["entries"]}
        self.assertEqual(names, {"notes.md", "readme.md"})

        viewed = self.client.post("/file", json={"path": "readme.md"})
        self.assertEqual(viewed.status_code, 200)
        self.assertIn("readme body", viewed.json()["content"])
        self.assertEqual(
            self.service.get_audit()[-1]["action"],
            "view_file",
        )


if __name__ == "__main__":
    unittest.main()
