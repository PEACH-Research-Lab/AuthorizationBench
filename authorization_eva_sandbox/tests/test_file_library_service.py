"""Smoke tests for services/file_library/service.py."""

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

from services.file_library.service import (  # noqa: E402
    FileLibraryService,
    create_service,
)


FIXTURE = {
    "library_name": "Acme Shared Drive",
    "current_user_id": "u-alex",
    "users": [
        {"id": "u-alex", "name": "Alex Morgan", "email": "alex@example.com"},
        {"id": "u-sam", "name": "Sam Rivera", "email": "sam@example.com"},
        {"id": "u-jordan", "name": "Jordan Lee", "email": "jordan@example.com"},
    ],
    "files": [
        {
            "id": "file-1",
            "name": "Q1 Budget.xlsx",
            "content": "budget rows",
            "owner_id": "u-alex",
            "shared_with": ["u-sam"],
            "created_at": "2025-01-10T08:00:00",
        },
        {
            "id": "file-2",
            "name": "Hiring Plan.docx",
            "content": "open roles",
            "owner_id": "u-jordan",
            "shared_with": [],
            "created_at": "2025-01-11T09:00:00",
        },
    ],
}


class FileLibraryServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["FILE_LIBRARY_FIXTURES"] = self._tmp.name
        self.svc = create_service("file_library")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/file_library")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("FILE_LIBRARY_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_files",
            "get_file",
            "upload_file",
            "share_file",
        }
        self.assertEqual(
            set(FileLibraryService.CAPABILITY_PROFILES["standard-v1"]),
            expected,
        )
        self.assertEqual(
            set(FileLibraryService.CAPABILITY_PROFILES["full-v1"]),
            expected,
        )
        self.assertEqual(set(FileLibraryService.ACTION_CONTRACTS), expected)
        for name in FileLibraryService._READ:
            contract = FileLibraryService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in FileLibraryService._MUTATE:
            contract = FileLibraryService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_get_upload_share(self) -> None:
        r = self.client.post("/file_library/files/list", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["library_name"], "Acme Shared Drive")
        self.assertEqual(len(body["files"]), 2)
        self.assertEqual(body["files"][0]["id"], "file-2")

        r = self.client.post(
            "/file_library/files/get",
            json={"file_id": "file-1"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["file"]["name"], "Q1 Budget.xlsx")
        self.assertEqual(r.json()["file"]["shared_with"], ["u-sam"])

        r = self.client.post(
            "/file_library/files/upload",
            json={"name": "Notes.txt", "content": "meeting notes"},
        )
        self.assertEqual(r.status_code, 200)
        created = r.json()["file"]
        self.assertEqual(created["name"], "Notes.txt")
        self.assertEqual(created["owner_id"], "u-alex")
        self.assertEqual(created["shared_with"], [])
        self.assertTrue(created["id"])

        r = self.client.post(
            "/file_library/files/share",
            json={"file_id": "file-2", "user_id": "u-alex"},
        )
        self.assertEqual(r.status_code, 200)
        shared = r.json()["file"]
        self.assertIn("u-alex", shared["shared_with"])

        state = self.client.get("/file_library/state").json()["state"]
        self.assertEqual(len(state["files"]), 3)
        self.assertEqual(
            next(f for f in state["files"] if f["id"] == "file-2")["shared_with"],
            ["u-alex"],
        )

        audit = self.client.get("/file_library/audit").json()["actions"]
        self.assertEqual(
            [e["action"] for e in audit],
            ["list_files", "get_file", "upload_file", "share_file"],
        )

    def test_missing_file_is_logged(self) -> None:
        r = self.client.post(
            "/file_library/files/get",
            json={"file_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "FILE_NOT_FOUND")
        audit = self.client.get("/file_library/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "get_file")
        self.assertEqual(audit[-1]["status"], 404)

    def test_share_missing_user_is_logged(self) -> None:
        r = self.client.post(
            "/file_library/files/share",
            json={"file_id": "file-1", "user_id": "u-missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "USER_NOT_FOUND")
        audit = self.client.get("/file_library/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "share_file")
        self.assertEqual(audit[-1]["status"], 404)


if __name__ == "__main__":
    unittest.main()
