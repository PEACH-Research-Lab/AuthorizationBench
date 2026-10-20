"""Smoke tests for services/support_desk/service.py."""

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

from services.support_desk.service import create_service  # noqa: E402
from services.registry import derive_actions  # noqa: E402

FIXTURE = {
    "company_name": "Cursor",
    "console_name": "Cursor Support Desk",
    "agent_display_name": "Lyra Support Bot",
    "agent_handle": "lyra-bot",
    "tickets": [
        {
            "id": "T-1001",
            "subject": "Refund not arriving",
            "customer_name": "Sam Rivera",
            "customer_email": "sam@example.com",
            "status": "open",
            "urgency": "high",
            "opened_ts": 1700000000.0,
            "messages": [
                {
                    "role": "customer",
                    "from": "Sam Rivera",
                    "body": "I still have not received my refund.",
                }
            ],
        }
    ],
    "kb_articles": [
        {
            "slug": "refund-timeline",
            "title": "Refund timeline",
            "summary": "Refunds post in 5-7 business days.",
            "body": "Approved refunds appear in 5-7 business days.",
            "tags": ["refund", "billing"],
        },
        {
            "slug": "password-reset",
            "title": "Password reset",
            "summary": "How to reset your password.",
            "body": "Use the account settings reset link.",
            "tags": ["account"],
        },
    ],
}


class SupportDeskServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["SUPPORT_DESK_FIXTURES"] = self._tmp.name
        self.svc = create_service("support_desk")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/support_desk")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("SUPPORT_DESK_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_module_imports_and_derived_actions(self) -> None:
        names = {a.name for a in derive_actions("support_desk")}
        self.assertEqual(
            names,
            {
                "list_tickets",
                "get_ticket",
                "search_kb",
                "send_reply",
                "escalate",
            },
        )
        profiles = self.svc.CAPABILITY_PROFILES
        for profile in ("standard-v1", "full-v1"):
            self.assertEqual(set(profiles[profile]), names)

    def test_list_get_and_search_kb(self) -> None:
        r = self.client.post("/support_desk/tickets/list")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tickets"][0]["id"], "T-1001")

        r = self.client.post(
            "/support_desk/tickets/get",
            json={"ticket_id": "T-1001"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ticket"]["subject"], "Refund not arriving")

        r = self.client.post(
            "/support_desk/kb/search",
            json={"query": "refund"},
        )
        self.assertEqual(r.status_code, 200)
        slugs = [a["slug"] for a in r.json()["articles"]]
        self.assertEqual(slugs, ["refund-timeline"])

    def test_send_reply_and_escalate(self) -> None:
        r = self.client.post(
            "/support_desk/tickets/reply",
            json={
                "ticket_id": "T-1001",
                "body": "Refunds post in 5-7 business days.",
                "cited_kb_slugs": ["refund-timeline"],
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["ticket"]["status"], "awaiting_customer")
        self.assertEqual(body["reply"]["body"], "Refunds post in 5-7 business days.")

        # Reset ticket to open for escalate path on a fresh service state.
        self.svc.tickets[0]["status"] = "open"
        r = self.client.post(
            "/support_desk/tickets/escalate",
            json={
                "ticket_id": "T-1001",
                "note": "Needs billing review",
                "queue": "tier-2",
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["ticket"]["status"], "escalated")
        self.assertEqual(body["escalation"]["queue"], "tier-2")

        audit = self.client.get("/support_desk/audit").json()["actions"]
        actions = [e["action"] for e in audit]
        self.assertEqual(actions, ["send_reply", "escalate"])

        state = self.client.get("/support_desk/state").json()["state"]
        self.assertEqual(len(state["replies"]), 1)
        self.assertEqual(len(state["escalations"]), 1)
        self.assertEqual(state["tickets"][0]["status"], "escalated")


if __name__ == "__main__":
    unittest.main()
