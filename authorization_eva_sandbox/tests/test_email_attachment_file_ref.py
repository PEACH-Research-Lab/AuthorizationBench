from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import authorization_eva_sandbox.services.email_client.service as email_service

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.email_client.service import (
    EmailClientService,
    _extract_pdf_text,
)
from services import _clock


class AttachmentFileRefTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        fixture = root / "email.json"
        fixture.write_text(json.dumps({"user_name": "Priya", "user_email": "priya@example.com",
                                       "emails": []}))
        self.workspace = root / "workspace"
        (self.workspace / "reports").mkdir(parents=True)
        (self.workspace / "reports" / "q1.csv").write_text("date,amount\n2025-01-07,12.00\n")
        (root / "secret.txt").write_text("outside")
        self.saved = {k: os.environ.get(k) for k in (fixture_env_var("mail"),
                                                     "SANDBOX_WORKSPACE_HOST_DIR")}
        os.environ[fixture_env_var("mail")] = str(fixture)
        os.environ["SANDBOX_WORKSPACE_HOST_DIR"] = str(self.workspace)
        self.service = EmailClientService("mail")
        self.service.load_data()
        app = FastAPI()
        app.include_router(self.service.router)
        self.client = TestClient(app)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tempdir.cleanup()

    def _send(self, attachment):
        return self.client.post("/messages/send", json={
            "to": ["auditor@example.com"], "subject": "Q1", "body": "attached",
            "attachments": [attachment]})

    def test_file_ref_attaches_the_workspace_file(self):
        r = self._send({"file_ref": "/workspace/reports/q1.csv"})
        self.assertEqual(r.status_code, 200, r.text)
        sent = [e for e in self.service.emails.values() if e.get("folder") == "sent"][-1]
        att = sent["attachments"][0]
        self.assertEqual(att["filename"], "q1.csv")
        self.assertIn("2025-01-07,12.00", att["content"])
        self.assertEqual(att["size"], len("date,amount\n2025-01-07,12.00\n"))

    def test_file_ref_outside_workspace_is_rejected(self):
        r = self._send({"file_ref": "/workspace/../secret.txt"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("under /workspace", r.text)

    def test_missing_file_is_rejected(self):
        r = self._send({"file_ref": "/workspace/reports/nope.csv"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no such file", r.text)

    def test_inline_content_still_works(self):
        r = self._send({"filename": "note.txt", "content": "hello"})
        self.assertEqual(r.status_code, 200, r.text)
        sent = [e for e in self.service.emails.values() if e.get("folder") == "sent"][-1]
        self.assertEqual(sent["attachments"][0]["content"], "hello")

    def test_no_attachments_is_fine(self):
        r = self.client.post("/messages/send", json={
            "to": ["auditor@example.com"], "subject": "Q1", "body": "no files"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_attachment_with_no_file_and_no_content_is_rejected(self):
        r = self._send({"filename": "q1.csv", "path": "/workspace/reports/q1.csv"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("file_ref", r.text)
        self.assertFalse([e for e in self.service.emails.values() if e.get("folder") == "sent"])

    def test_empty_workspace_file_is_rejected(self):
        (self.workspace / "empty.txt").write_text("")
        r = self._send({"file_ref": "/workspace/empty.txt"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("empty file", r.text)

    def test_empty_attachment_on_draft_is_rejected(self):
        r = self.client.post("/drafts/create", json={
            "to": ["auditor@example.com"], "subject": "Q1",
            "attachments": [{"filename": "q1.csv"}]})
        self.assertEqual(r.status_code, 422)
        self.assertIn("file_ref", r.text)

    def _audit_attachment(self, action="send_message"):
        sent = [entry for entry in self.service.get_audit() if entry.get("action") == action]
        self.assertTrue(sent, self.service.get_audit())
        return sent[-1]["attachment_contents"][0]

    def test_text_attachment_log_is_unchanged(self):
        r = self._send({"file_ref": "/workspace/reports/q1.csv"})
        self.assertEqual(r.status_code, 200, r.text)
        logged = self._audit_attachment()
        self.assertEqual(set(logged), {"filename", "size", "content", "truncated"})
        self.assertEqual(logged["content"], "date,amount\n2025-01-07,12.00\n")
        self.assertFalse(logged["truncated"])
        self.assertNotIn("content_base64", logged)

    def test_pdf_attachment_logs_exact_bytes_and_text(self):
        pdf = _pdf_bytes("Confidential panel report")
        (self.workspace / "reports" / "panel.pdf").write_bytes(pdf)
        r = self._send({"file_ref": "/workspace/reports/panel.pdf"})
        self.assertEqual(r.status_code, 200, r.text)
        shown = r.json()["message"]["attachments"][0]
        self.assertNotIn("content_base64", shown)
        self.assertIsInstance(shown["content"], str)
        logged = self._audit_attachment()
        self.assertEqual(base64.b64decode(logged["content_base64"]), pdf)
        self.assertEqual(logged["media_type"], "application/pdf")
        self.assertEqual(logged["size"], len(pdf))
        self.assertIn("Confidential panel report", logged["text"])
        self.assertFalse(logged["truncated"])
        self.assertNotIn("content", logged)

    def test_pdf_log_truncation_flag_matches_the_stored_prefix(self):
        pdf = _pdf_bytes("Confidential panel report")
        (self.workspace / "reports" / "panel.pdf").write_bytes(pdf)
        original = email_service.MAX_ATTACHMENT_LOG_BYTES
        email_service.MAX_ATTACHMENT_LOG_BYTES = 32
        try:
            r = self._send({"file_ref": "/workspace/reports/panel.pdf"})
        finally:
            email_service.MAX_ATTACHMENT_LOG_BYTES = original
        self.assertEqual(r.status_code, 200, r.text)
        logged = self._audit_attachment()
        self.assertTrue(logged["truncated"])
        self.assertEqual(base64.b64decode(logged["content_base64"]), pdf[:32])
        self.assertEqual(logged["size"], len(pdf))
        self.assertIn("Confidential panel report", logged["text"])

    def test_draft_send_logs_the_pdf_that_was_attached(self):
        pdf = _pdf_bytes("Drafted panel report")
        (self.workspace / "reports" / "draft.pdf").write_bytes(pdf)
        created = self.client.post("/drafts/create", json={
            "to": ["auditor@example.com"], "subject": "Q1",
            "attachments": [{"file_ref": "/workspace/reports/draft.pdf"}]})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertNotIn("content_base64", created.json()["draft"]["attachments"][0])
        sent = self.client.post("/drafts/send", json={"draft_id": created.json()["draft"]["id"]})
        self.assertEqual(sent.status_code, 200, sent.text)
        logged = self._audit_attachment("send_draft")
        self.assertEqual(base64.b64decode(logged["content_base64"]), pdf)
        self.assertIn("Drafted panel report", logged["text"])

    def test_pdf_text_is_empty_when_pypdf_is_missing(self):
        class _Finder:
            def find_spec(self, fullname, path, target=None):
                if fullname == "pypdf" or fullname.startswith("pypdf."):
                    raise ImportError("pypdf blocked")
                return None

        finder = _Finder()
        saved = sys.modules.pop("pypdf", None)
        sys.meta_path.insert(0, finder)
        try:
            self.assertEqual(_extract_pdf_text(b"%PDF-1.4\n"), "")
        finally:
            sys.meta_path.remove(finder)
            if saved is not None:
                sys.modules["pypdf"] = saved

    def test_draft_with_file_ref(self):
        r = self.client.post("/drafts/create", json={
            "to": ["auditor@example.com"], "subject": "Q1",
            "attachments": [{"file_ref": "reports/q1.csv"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("2025-01-07", r.json()["draft"]["attachments"][0]["content"])


def _pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    header = b"%PDF-1.4\n"
    body = b""
    offsets = []
    cursor = len(header)
    for part in objects:
        offsets.append(cursor)
        body += part
        cursor += len(part)
    xref = b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{cursor}\n%%EOF\n".encode()
    return header + body + xref + trailer


if __name__ == "__main__":
    unittest.main()
