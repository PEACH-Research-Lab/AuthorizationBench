"""Smoke tests for services/card_payment/service.py."""

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

from services.card_payment.service import (  # noqa: E402
    CardPaymentService,
    create_service,
)


FIXTURE = {
    "app_name": "Ledger Card",
    "merchant_name": "River Merchants",
    "default_currency": "USD",
    "charges": [
        {
            "id": "ch-0",
            "amount": 25.0,
            "currency": "USD",
            "status": "succeeded",
            "description": "Starter kit",
            "customer_email": "buyer@example.com",
            "card_last4": "4242",
            "card_brand": "visa",
            "amount_refunded": 0.0,
            "created_at": "2025-01-10T09:00:00",
            "refunds": [],
        },
        {
            "id": "ch-1",
            "amount": 100.0,
            "currency": "USD",
            "status": "partially_refunded",
            "description": "Consulting retainer",
            "customer_email": "client@example.com",
            "card_last4": "4444",
            "card_brand": "mastercard",
            "amount_refunded": 20.0,
            "created_at": "2025-01-12T14:00:00",
            "refunds": [
                {
                    "id": "rf-0",
                    "amount": 20.0,
                    "reason": "partial credit",
                    "created_at": "2025-01-12T15:00:00",
                    "status": "succeeded",
                }
            ],
        },
    ],
}


class CardPaymentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["CARD_PAYMENT_FIXTURES"] = self._tmp.name
        self.svc = create_service("card_payment")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/card_payment")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("CARD_PAYMENT_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_charges",
            "get_charge",
            "create_charge",
            "refund_charge",
        }
        self.assertEqual(
            set(CardPaymentService.CAPABILITY_PROFILES["standard-v1"]),
            expected,
        )
        self.assertEqual(
            set(CardPaymentService.CAPABILITY_PROFILES["full-v1"]),
            expected,
        )
        self.assertEqual(set(CardPaymentService.ACTION_CONTRACTS), expected)
        for name in CardPaymentService._READ:
            contract = CardPaymentService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in CardPaymentService._MUTATE:
            contract = CardPaymentService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_and_get_charges(self) -> None:
        r = self.client.post(
            "/card_payment/charges/list",
            json={"limit": 1},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["charges"]), 1)
        self.assertEqual(body["charges"][0]["id"], "ch-1")

        r = self.client.post(
            "/card_payment/charges/get",
            json={"charge_id": "ch-0"},
        )
        self.assertEqual(r.status_code, 200)
        charge = r.json()["charge"]
        self.assertEqual(charge["amount"], 25.0)
        self.assertEqual(charge["card_last4"], "4242")

    def test_create_and_refund_charge(self) -> None:
        r = self.client.post(
            "/card_payment/charges/create",
            json={
                "amount": 49.99,
                "currency": "usd",
                "description": "Widget",
                "customer_email": "shopper@example.com",
                "card_last4": "1111",
                "card_brand": "visa",
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        charge = body["charge"]
        self.assertEqual(charge["status"], "succeeded")
        self.assertEqual(charge["amount"], 49.99)
        self.assertEqual(charge["currency"], "USD")
        charge_id = charge["id"]

        r = self.client.post(
            "/card_payment/charges/refund",
            json={"charge_id": charge_id, "amount": 10.0, "reason": "partial"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["refund"]["amount"], 10.0)
        self.assertEqual(body["charge"]["amount_refunded"], 10.0)
        self.assertEqual(body["charge"]["status"], "partially_refunded")
        refund_id = body["refund"]["id"]

        r = self.client.post(
            "/card_payment/charges/refund",
            json={"charge_id": charge_id},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["refund"]["amount"], 39.99)
        self.assertEqual(body["charge"]["status"], "refunded")
        self.assertEqual(body["charge"]["amount_refunded"], 49.99)

        state = self.client.get("/card_payment/state").json()["state"]
        created = next(c for c in state["charges"] if c["id"] == charge_id)
        self.assertEqual(len(created["refunds"]), 2)
        self.assertEqual(created["refunds"][0]["id"], refund_id)

        audit = self.client.get("/card_payment/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "refund_charge")
        self.assertEqual(audit[-1]["status"], 200)

    def test_missing_and_invalid_are_logged(self) -> None:
        r = self.client.post(
            "/card_payment/charges/get",
            json={"charge_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "CHARGE_NOT_FOUND")

        r = self.client.post(
            "/card_payment/charges/create",
            json={"amount": 0},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "INVALID_AMOUNT")

        r = self.client.post(
            "/card_payment/charges/refund",
            json={"charge_id": "ch-1", "amount": 999.0},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "REFUND_EXCEEDS_REMAINING")

        r = self.client.post(
            "/card_payment/charges/refund",
            json={"charge_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        audit = self.client.get("/card_payment/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "refund_charge")
        self.assertEqual(audit[-1]["status"], 404)


if __name__ == "__main__":
    unittest.main()
