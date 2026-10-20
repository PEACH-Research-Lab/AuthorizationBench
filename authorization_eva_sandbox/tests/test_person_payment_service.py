"""Smoke tests for services/person_payment/service.py."""

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

from services.person_payment.service import (  # noqa: E402
    PersonPaymentService,
    create_service,
)


FIXTURE = {
    "app_name": "Peer Pay",
    "owner_name": "Alex Morgan",
    "owner_handle": "alexm",
    "friends": [
        {
            "id": "fr-1",
            "display_name": "Sam Rivera",
            "handle": "samr",
        },
        {
            "id": "fr-2",
            "display_name": "Jordan Lee",
            "handle": "jlee",
        },
    ],
    "payments": [
        {
            "id": "pay-0",
            "friend_id": "fr-2",
            "friend_handle": "jlee",
            "friend_display_name": "Jordan Lee",
            "amount": 12.5,
            "note": "coffee",
            "kind": "pay",
            "direction": "outbound",
            "status": "completed",
            "timestamp": "2025-01-10T09:00:00",
        }
    ],
}


class PersonPaymentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["PERSON_PAYMENT_FIXTURES"] = self._tmp.name
        self.svc = create_service("person_payment")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/person_payment")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("PERSON_PAYMENT_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_friends",
            "list_payments",
            "pay_friend",
            "request_money",
        }
        self.assertEqual(
            set(PersonPaymentService.CAPABILITY_PROFILES["standard-v1"]),
            expected,
        )
        self.assertEqual(
            set(PersonPaymentService.CAPABILITY_PROFILES["full-v1"]),
            expected,
        )
        self.assertEqual(set(PersonPaymentService.ACTION_CONTRACTS), expected)
        for name in PersonPaymentService._READ:
            contract = PersonPaymentService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in PersonPaymentService._MUTATE:
            contract = PersonPaymentService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_friends_and_payments(self) -> None:
        r = self.client.post("/person_payment/friends/list")
        self.assertEqual(r.status_code, 200)
        ids = [f["id"] for f in r.json()["friends"]]
        self.assertEqual(ids, ["fr-2", "fr-1"])

        r = self.client.post("/person_payment/payments/list")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["payments"]), 1)
        self.assertEqual(r.json()["payments"][0]["id"], "pay-0")

    def test_pay_friend_records_completed(self) -> None:
        r = self.client.post(
            "/person_payment/payments/pay",
            json={"friend_id": "fr-1", "amount": 20.0, "note": "lunch"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["payment"]["kind"], "pay")
        self.assertEqual(body["payment"]["status"], "completed")
        self.assertEqual(body["payment"]["friend_id"], "fr-1")
        self.assertEqual(body["payment"]["amount"], 20.0)
        payment_id = body["payment"]["id"]

        state = self.client.get("/person_payment/state").json()["state"]
        self.assertEqual(len(state["payments"]), 2)
        self.assertEqual(state["payments"][-1]["id"], payment_id)

        audit = self.client.get("/person_payment/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "pay_friend")
        self.assertEqual(audit[-1]["status"], 200)

    def test_request_money_records_pending(self) -> None:
        r = self.client.post(
            "/person_payment/payments/request",
            json={"friend_id": "fr-1", "amount": 8.25, "note": "tickets"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["payment"]["kind"], "request")
        self.assertEqual(body["payment"]["status"], "pending")
        self.assertEqual(body["payment"]["friend_id"], "fr-1")
        self.assertEqual(body["payment"]["amount"], 8.25)

        state = self.client.get("/person_payment/state").json()["state"]
        self.assertEqual(len(state["payments"]), 2)
        self.assertEqual(state["payments"][-1]["kind"], "request")

    def test_missing_friend_and_invalid_amount(self) -> None:
        r = self.client.post(
            "/person_payment/payments/pay",
            json={"friend_id": "missing", "amount": 5.0},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "FRIEND_NOT_FOUND")

        r = self.client.post(
            "/person_payment/payments/request",
            json={"friend_id": "missing", "amount": 5.0},
        )
        self.assertEqual(r.status_code, 404)
        audit = self.client.get("/person_payment/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "request_money")
        self.assertEqual(audit[-1]["status"], 404)

        r = self.client.post(
            "/person_payment/payments/pay",
            json={"friend_id": "fr-1", "amount": 0},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "INVALID_AMOUNT")


if __name__ == "__main__":
    unittest.main()
