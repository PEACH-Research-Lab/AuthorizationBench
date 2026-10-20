"""Smoke tests for services/bank_account/service.py."""

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

from services.bank_account.service import (  # noqa: E402
    BankAccountService,
    create_service,
)


FIXTURE = {
    "app_name": "River Bank",
    "account_name": "Everyday Checking",
    "account_number": "US00TEST00000001",
    "currency": "USD",
    "balance": 500.0,
    "payees": [
        {
            "id": "payee-power",
            "name": "City Power",
            "account_number": "BILL-POWER-001",
            "category": "utilities",
        },
        {
            "id": "payee-isp",
            "name": "FiberNet ISP",
            "account_number": "BILL-ISP-009",
            "category": "internet",
        },
    ],
    "transactions": [
        {
            "id": "txn-0",
            "kind": "credit",
            "amount": 200.0,
            "counterparty": "Payroll",
            "counterparty_account": "EMP-ACME",
            "description": "Biweekly payroll",
            "timestamp": "2025-01-10T09:00:00",
            "status": "posted",
        },
        {
            "id": "txn-1",
            "kind": "debit",
            "amount": 40.0,
            "counterparty": "Cafe",
            "counterparty_account": "",
            "description": "Coffee",
            "timestamp": "2025-01-12T14:00:00",
            "status": "posted",
        },
    ],
}


class BankAccountServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["BANK_ACCOUNT_FIXTURES"] = self._tmp.name
        self.svc = create_service("bank_account")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/bank_account")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("BANK_ACCOUNT_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "get_balance",
            "list_transactions",
            "transfer",
            "pay_bill",
        }
        self.assertEqual(
            set(BankAccountService.CAPABILITY_PROFILES["standard-v1"]),
            expected,
        )
        self.assertEqual(
            set(BankAccountService.CAPABILITY_PROFILES["full-v1"]),
            expected,
        )
        self.assertEqual(set(BankAccountService.ACTION_CONTRACTS), expected)
        for name in BankAccountService._READ:
            contract = BankAccountService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in BankAccountService._MUTATE:
            contract = BankAccountService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_balance_and_list_transactions(self) -> None:
        r = self.client.post("/bank_account/balance")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["balance"], 500.0)
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["account_number"], "US00TEST00000001")

        r = self.client.post(
            "/bank_account/transactions/list",
            json={"limit": 1},
        )
        self.assertEqual(r.status_code, 200)
        txns = r.json()["transactions"]
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0]["id"], "txn-1")

    def test_transfer_and_pay_bill(self) -> None:
        r = self.client.post(
            "/bank_account/transfer",
            json={
                "to_account": "US99FRIEND00002",
                "amount": 50.0,
                "memo": "Lunch split",
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["transaction"]["kind"], "transfer")
        self.assertEqual(body["balance"], 450.0)
        transfer_id = body["transaction"]["id"]

        r = self.client.post(
            "/bank_account/bills/pay",
            json={"payee_id": "payee-power", "amount": 75.25},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["transaction"]["kind"], "bill_pay")
        self.assertEqual(body["transaction"]["counterparty"], "City Power")
        self.assertEqual(body["balance"], 374.75)

        state = self.client.get("/bank_account/state").json()["state"]
        self.assertEqual(state["balance"], 374.75)
        self.assertEqual(len(state["transactions"]), 4)
        self.assertEqual(state["transactions"][-2]["id"], transfer_id)

        audit = self.client.get("/bank_account/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "pay_bill")
        self.assertEqual(audit[-1]["status"], 200)

    def test_missing_payee_and_insufficient_funds_logged(self) -> None:
        r = self.client.post(
            "/bank_account/bills/pay",
            json={"payee_id": "missing", "amount": 10.0},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "PAYEE_NOT_FOUND")

        r = self.client.post(
            "/bank_account/transfer",
            json={"to_account": "US99FRIEND00002", "amount": 9999.0},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "INSUFFICIENT_BALANCE")

        audit = self.client.get("/bank_account/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "transfer")
        self.assertEqual(audit[-1]["status"], 400)
        self.assertEqual(audit[-2]["action"], "pay_bill")
        self.assertEqual(audit[-2]["status"], 404)


if __name__ == "__main__":
    unittest.main()
