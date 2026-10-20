from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.email_client.service import (
    EmailClientService,
)
from services import _clock


class EmailDraftContractTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        fixture = Path(self.tempdir.name) / "email.json"
        fixture.write_text(json.dumps({
            "user_name": "Priya",
            "user_email": "priya@example.com",
            "emails": [],
        }))
        self.env_key = fixture_env_var("mail")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = EmailClientService("mail")
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

    @staticmethod
    def draft_payload() -> dict:
        return {
            "to": ["team@example.com"],
            "cc": ["manager@example.com"],
            "bcc": [],
            "subject": "Meeting summary",
            "body": "Initial body",
            "attachments": [{
                "filename": "../minutes.md",
                "media_type": "text/markdown",
                "content": "Minutes",
            }],
        }

    def test_create_update_send_draft_lifecycle(self):
        created = self.client.post(
            "/drafts/create", json=self.draft_payload()
        )
        self.assertEqual(created.status_code, 200)
        draft = created.json()["draft"]
        draft_id = draft["id"]
        self.assertRegex(draft_id, r"^message-[0-9a-f]{10}$")
        self.assertRegex(draft["thread_id"], r"^thread-[0-9a-f]{10}$")
        self.assertEqual(draft["folder"], "drafts")
        self.assertEqual(draft["to"], ["team@example.com"])
        self.assertEqual(draft["attachments"][0], {
            "filename": "minutes.md",
            "media_type": "text/markdown",
            "size": 7,
            "content": "Minutes",
            "file_ref": "",
        })
        self.assertEqual(
            draft["created_at"], "2026-06-06T10:11:00"
        )

        updated = self.client.post("/drafts/update", json={
            "draft_id": draft_id,
            "body": "Final body",
            "to": ["team@example.com", "ops@example.com"],
        })
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["draft"]["body"], "Final body")

        sent = self.client.post(
            "/drafts/send", json={"draft_id": draft_id}
        )
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.json()["message"]["folder"], "sent")
        state = self.service.get_state()
        self.assertEqual(
            [item["id"] for item in state["emails"]
             if item["folder"] == "sent"],
            [draft_id],
        )
        self.assertEqual(
            [item for item in state["emails"]
             if item["folder"] == "drafts"],
            [],
        )
        self.assertEqual(
            [entry["action"] for entry in self.service.get_audit()],
            ["create_draft", "update_draft", "send_draft"],
        )

    def test_delete_and_failed_attempts_are_audited(self):
        created = self.client.post(
            "/drafts/create", json=self.draft_payload()
        ).json()["draft"]
        deleted = self.client.post(
            "/drafts/delete", json={"draft_id": created["id"]}
        )
        self.assertEqual(deleted.status_code, 200)
        missing = self.client.post(
            "/drafts/delete", json={"draft_id": created["id"]}
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.json()["error"]["code"], "DRAFT_NOT_FOUND"
        )
        audit = self.service.get_audit()
        self.assertEqual(audit[-1]["action"], "delete_draft")
        self.assertEqual(audit[-1]["status"], 404)

    def test_recipient_validation_and_reset_are_deterministic(self):
        invalid = self.client.post("/drafts/create", json={
            **self.draft_payload(),
            "to": ["not-an-address"],
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            invalid.json()["error"]["code"], "INVALID_DRAFT"
        )

        first = self.client.post(
            "/drafts/create", json=self.draft_payload()
        ).json()["draft"]["id"]
        self.service.reset()
        second = self.client.post(
            "/drafts/create", json=self.draft_payload()
        ).json()["draft"]["id"]
        self.assertEqual(first, second)
        self.assertEqual(
            [entry["action"] for entry in self.service.get_audit()],
            ["create_draft"],
        )

    def _seed_thread(self) -> None:
        for index, body in enumerate(("First", "Second"), start=1):
            self.service._inject({
                "id": f"mail-{index}",
                "thread_id": "thread-shared",
                "from": "sender@example.com",
                "to": ["priya@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Thread",
                "body": body,
                "folder": "inbox",
                "read": False,
                "starred": False,
                "priority": "normal",
                "timestamp": f"2026-06-06T10:{index:02d}:00",
                "attachments": [],
            })

    def test_list_get_and_thread_are_non_mutating(self):
        self._seed_thread()
        listed = self.client.post("/messages/list", json={
            "folder": "inbox",
            "sort": "date_asc",
            "page": 1,
            "page_size": 1,
            "read": False,
        })
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total_matches"], 2)
        self.assertEqual(
            listed.json()["messages"][0]["id"], "mail-1"
        )
        message = self.client.post(
            "/messages/get", json={"message_id": "mail-1"}
        )
        self.assertFalse(message.json()["message"]["read"])
        self.assertFalse(self.service.emails["mail-1"]["read"])
        thread = self.client.post(
            "/threads/get", json={"thread_id": "thread-shared"}
        )
        self.assertEqual(
            [item["id"] for item in thread.json()["messages"]],
            ["mail-1", "mail-2"],
        )

    def test_modify_is_idempotent_and_bulk_modify_is_atomic(self):
        self._seed_thread()
        first = self.client.post("/messages/modify", json={
            "message_id": "mail-1",
            "read": True,
            "starred": True,
        })
        self.assertEqual(first.json()["status"], "modified")
        second = self.client.post("/messages/modify", json={
            "message_id": "mail-1",
            "read": True,
            "starred": True,
        })
        self.assertEqual(second.json()["status"], "unchanged")
        self.assertTrue(self.service.get_audit()[-1]["no_op"])

        failed = self.client.post("/messages/bulk_modify", json={
            "message_ids": ["mail-1", "missing"],
            "folder": "archive",
        })
        self.assertEqual(failed.status_code, 404)
        self.assertEqual(
            self.service.emails["mail-1"]["folder"], "inbox"
        )
        changed = self.client.post("/messages/bulk_modify", json={
            "message_ids": ["mail-1", "mail-2"],
            "folder": "archive",
        })
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(
            {item["folder"] for item in self.service.emails.values()},
            {"archive"},
        )

    def test_send_message_uses_canonical_recipients(self):
        sent = self.client.post("/messages/send", json={
            "to": ["team@example.com"],
            "cc": ["manager@example.com"],
            "bcc": [],
            "subject": "Status",
            "body": "Done",
            "attachments": [],
        })
        self.assertEqual(sent.status_code, 200)
        message = sent.json()["message"]
        self.assertEqual(message["to"], ["team@example.com"])
        self.assertEqual(message["cc"], ["manager@example.com"])
        self.assertEqual(message["folder"], "sent")
        self.assertEqual(
            self.service.get_audit()[-1]["action"], "send_message"
        )


if __name__ == "__main__":
    unittest.main()
