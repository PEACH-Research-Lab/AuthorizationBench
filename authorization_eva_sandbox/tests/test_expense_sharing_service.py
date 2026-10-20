"""Smoke tests for services/expense_sharing/service.py."""

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

from services.expense_sharing.service import (  # noqa: E402
    ExpenseSharingService,
    create_service,
)


FIXTURE = {
    "app_name": "Split Bench",
    "current_user_id": "u-alex",
    "users": [
        {"id": "u-alex", "name": "Alex Morgan", "email": "alex@example.com"},
        {"id": "u-sam", "name": "Sam Rivera", "email": "sam@example.com"},
        {"id": "u-jordan", "name": "Jordan Lee", "email": "jordan@example.com"},
    ],
    "groups": [
        {
            "id": "g-roommates",
            "name": "Roommates",
            "member_ids": ["u-alex", "u-sam", "u-jordan"],
            "created_at": "2025-01-01T00:00:00Z",
        },
        {
            "id": "g-trip",
            "name": "Weekend Trip",
            "member_ids": ["u-alex", "u-sam"],
            "created_at": "2025-02-01T00:00:00Z",
        },
    ],
    "expenses": [
        {
            "id": "e-rent",
            "group_id": "g-roommates",
            "description": "January rent",
            "amount": 300.0,
            "currency": "USD",
            "paid_by": "u-alex",
            "split_equally": True,
            "shares": [
                {"user_id": "u-alex", "owed_share": 100.0, "paid_share": 300.0},
                {"user_id": "u-sam", "owed_share": 100.0, "paid_share": 0.0},
                {"user_id": "u-jordan", "owed_share": 100.0, "paid_share": 0.0},
            ],
            "status": "open",
            "created_at": "2025-01-02T00:00:00Z",
            "settled_at": "",
        }
    ],
}


class ExpenseSharingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["EXPENSE_SHARING_FIXTURES"] = self._tmp.name
        self.svc = create_service("expense_sharing")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/expense_sharing")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("EXPENSE_SHARING_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_groups",
            "get_group",
            "add_expense",
            "settle_expense",
        }
        self.assertEqual(
            set(ExpenseSharingService.CAPABILITY_PROFILES["standard-v1"]),
            expected,
        )
        self.assertEqual(
            set(ExpenseSharingService.CAPABILITY_PROFILES["full-v1"]),
            expected,
        )
        self.assertEqual(set(ExpenseSharingService.ACTION_CONTRACTS), expected)
        for name in ExpenseSharingService._READ:
            contract = ExpenseSharingService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in ExpenseSharingService._MUTATE:
            contract = ExpenseSharingService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_get_add_settle(self) -> None:
        r = self.client.post("/expense_sharing/groups/list")
        self.assertEqual(r.status_code, 200)
        groups = r.json()["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["id"], "g-roommates")
        self.assertEqual(groups[0]["open_expense_count"], 1)

        r = self.client.post(
            "/expense_sharing/groups/get",
            json={"group_id": "g-roommates"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["group"]["name"], "Roommates")
        self.assertEqual(len(body["group"]["members"]), 3)
        self.assertEqual(len(body["group"]["expenses"]), 1)
        balances = {
            b["user_id"]: b["net_balance"] for b in body["group"]["balances"]
        }
        self.assertEqual(balances["u-alex"], 200.0)
        self.assertEqual(balances["u-sam"], -100.0)
        self.assertEqual(balances["u-jordan"], -100.0)

        r = self.client.post(
            "/expense_sharing/expenses/add",
            json={
                "group_id": "g-trip",
                "description": "Gas",
                "amount": 40.0,
                "paid_by": "u-sam",
            },
        )
        self.assertEqual(r.status_code, 200)
        expense = r.json()["expense"]
        self.assertTrue(r.json()["ok"])
        self.assertEqual(expense["status"], "open")
        self.assertEqual(expense["amount"], 40.0)
        self.assertEqual(expense["group_id"], "g-trip")
        self.assertEqual(len(expense["shares"]), 2)
        expense_id = expense["id"]

        r = self.client.post(
            "/expense_sharing/expenses/settle",
            json={"expense_id": "e-rent"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["expense"]["status"], "settled")
        self.assertTrue(r.json()["expense"]["settled_at"])

        state = self.client.get("/expense_sharing/state").json()["state"]
        self.assertEqual(len(state["expenses"]), 2)
        rent = next(e for e in state["expenses"] if e["id"] == "e-rent")
        self.assertEqual(rent["status"], "settled")
        gas = next(e for e in state["expenses"] if e["id"] == expense_id)
        self.assertEqual(gas["status"], "open")

        audit = self.client.get("/expense_sharing/audit").json()["actions"]
        self.assertEqual(
            [e["action"] for e in audit],
            ["list_groups", "get_group", "add_expense", "settle_expense"],
        )

    def test_missing_group_and_expense_are_logged(self) -> None:
        r = self.client.post(
            "/expense_sharing/groups/get",
            json={"group_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "GROUP_NOT_FOUND")

        r = self.client.post(
            "/expense_sharing/expenses/settle",
            json={"expense_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EXPENSE_NOT_FOUND")

        audit = self.client.get("/expense_sharing/audit").json()["actions"]
        self.assertEqual(audit[0]["action"], "get_group")
        self.assertEqual(audit[0]["status"], 404)
        self.assertEqual(audit[1]["action"], "settle_expense")
        self.assertEqual(audit[1]["status"], 404)

    def test_settle_already_settled_is_409(self) -> None:
        self.client.post(
            "/expense_sharing/expenses/settle",
            json={"expense_id": "e-rent"},
        )
        r = self.client.post(
            "/expense_sharing/expenses/settle",
            json={"expense_id": "e-rent"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "ALREADY_SETTLED")


if __name__ == "__main__":
    unittest.main()
