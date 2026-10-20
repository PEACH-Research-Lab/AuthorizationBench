"""Order-aftercare gap coverage for amazon_shop (AppWorld / tau-bench retail)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from authorization_eva_sandbox.services._base import fixture_env_var
from authorization_eva_sandbox.services.amazon_shop.service import AmazonShopService
from authorization_eva_sandbox.services.registry import ServiceRegistry
from services import _clock


EXISTING_ACTIONS = frozenset({
    "search_products", "view_product", "add_to_cart", "remove_from_cart",
    "update_cart_quantity", "view_cart", "begin_checkout", "buy_now",
    "place_order", "get_order",
})

NEW_ORDER_ACTIONS = frozenset({
    "update_order_address", "cancel_order", "return_order", "exchange_order",
})


class AmazonOrderGapTests(unittest.TestCase):
    def setUp(self):
        _clock.configure("2026-06-06T10:11:00Z")
        self.tempdir = tempfile.TemporaryDirectory()
        fixture = Path(self.tempdir.name) / "shop.json"
        fixture.write_text(json.dumps({
            "user_name": "Alex Morgan",
            "user_address": "1 Market St",
            "payment_method": "Visa ****1111",
            "deal_active_initial": False,
            "deal_label": "Limited time deal",
            "agent_name": "",
            "shipping_name_default": "user",
            "address_options": [],
            "address_default_key": "",
            "products": [
                {
                    "id": "prod-a",
                    "name": "Wireless Mouse",
                    "brand": "Acme",
                    "category": "electronics",
                    "tags": ["mouse"],
                    "regular_price": 25.0,
                    "price": 25.0,
                },
                {
                    "id": "prod-b",
                    "name": "USB-C Hub",
                    "brand": "Acme",
                    "category": "electronics",
                    "tags": ["hub"],
                    "regular_price": 40.0,
                    "price": 40.0,
                },
            ],
            "orders": [
                {
                    "order_id": "ORD-UNSHIPPED",
                    "items": [{"id": "prod-a", "name": "Wireless Mouse",
                               "price": 25.0, "quantity": 1, "line_total": 25.0}],
                    "subtotal": 25.0,
                    "shipping": 0.0,
                    "tax": 2.0,
                    "total": 27.0,
                    "address": "1 Market St",
                    "shipping_name": "Alex Morgan",
                    "payment": "Visa ****1111",
                    "status": "unshipped",
                    "timestamp": "2026-06-05T09:00:00Z",
                },
                {
                    "order_id": "ORD-DELIVERED",
                    "items": [{"id": "prod-a", "name": "Wireless Mouse",
                               "price": 25.0, "quantity": 1, "line_total": 25.0}],
                    "subtotal": 25.0,
                    "shipping": 0.0,
                    "tax": 2.0,
                    "total": 27.0,
                    "address": "1 Market St",
                    "shipping_name": "Alex Morgan",
                    "payment": "Visa ****1111",
                    "status": "delivered",
                    "timestamp": "2026-06-01T09:00:00Z",
                },
                {
                    "order_id": "ORD-SHIPPED",
                    "items": [{"id": "prod-a", "name": "Wireless Mouse",
                               "price": 25.0, "quantity": 1, "line_total": 25.0}],
                    "subtotal": 25.0,
                    "shipping": 0.0,
                    "tax": 2.0,
                    "total": 27.0,
                    "address": "1 Market St",
                    "shipping_name": "Alex Morgan",
                    "payment": "Visa ****1111",
                    "status": "shipped",
                    "timestamp": "2026-06-04T09:00:00Z",
                },
            ],
        }))
        self.env_key = fixture_env_var("shop-order-gaps")
        self.previous = os.environ.get(self.env_key)
        os.environ[self.env_key] = str(fixture)
        self.service = AmazonShopService("shop-order-gaps")
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
        spec = ServiceRegistry.load(derive_live=True).templates["amazon_shop"]
        live = {action.name for action in spec.actions}
        self.assertTrue(EXISTING_ACTIONS.issubset(live))
        self.assertTrue(NEW_ORDER_ACTIONS.issubset(live))
        self.assertEqual(live, EXISTING_ACTIONS | NEW_ORDER_ACTIONS)

        standard = set(spec.capability_profiles["standard-v1"])
        full = set(spec.capability_profiles["full-v1"])
        self.assertTrue(EXISTING_ACTIONS.issubset(standard))
        self.assertTrue(NEW_ORDER_ACTIONS.issubset(standard))
        self.assertTrue(EXISTING_ACTIONS.issubset(full))
        self.assertTrue(NEW_ORDER_ACTIONS.issubset(full))

        for name in EXISTING_ACTIONS | NEW_ORDER_ACTIONS:
            self.assertIn(name, AmazonShopService.ACTION_CONTRACTS)

    def test_new_actions_are_state_changing_mutations(self):
        for name in NEW_ORDER_ACTIONS:
            contract = AmazonShopService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

        for name in ("search_products", "view_product", "view_cart", "get_order"):
            contract = AmazonShopService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)

    def test_update_order_address_on_unshipped(self):
        response = self.client.post("/orders/update_address", json={
            "order_id": "ORD-UNSHIPPED",
            "address": "99 Harbor Rd",
            "shipping_name": "Alex Morgan",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "updated")
        self.assertEqual(body["order"]["address"], "99 Harbor Rd")
        stored = self.service._find_order("ORD-UNSHIPPED")
        self.assertEqual(stored["address"], "99 Harbor Rd")
        self.assertEqual(self.service.get_audit()[-1]["action"], "update_order_address")

    def test_cancel_order_only_unshipped(self):
        ok = self.client.post("/orders/cancel", json={
            "order_id": "ORD-UNSHIPPED",
            "reason": "ordered by mistake",
        })
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["status"], "cancelled")
        self.assertEqual(ok.json()["order"]["status"], "cancelled")

        blocked = self.client.post("/orders/cancel", json={
            "order_id": "ORD-SHIPPED",
            "reason": "too late",
        })
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "ORDER_NOT_UNSHIPPED")
        self.assertEqual(self.service.get_audit()[-1]["status"], 409)

    def test_return_order_only_delivered(self):
        ok = self.client.post("/orders/return", json={
            "order_id": "ORD-DELIVERED",
            "reason": "defective",
        })
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["status"], "returned")
        self.assertEqual(ok.json()["order"]["status"], "returned")

        blocked = self.client.post("/orders/return", json={
            "order_id": "ORD-SHIPPED",
        })
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "ORDER_NOT_DELIVERED")

    def test_exchange_order_replaces_items(self):
        response = self.client.post("/orders/exchange", json={
            "order_id": "ORD-DELIVERED",
            "new_product_id": "prod-b",
            "quantity": 1,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "exchanged")
        self.assertEqual(body["order"]["status"], "exchanged")
        self.assertEqual(body["order"]["items"][0]["id"], "prod-b")
        self.assertEqual(body["order"]["exchanged_for_product_id"], "prod-b")
        self.assertTrue(body["order"]["prior_items"])

    def test_missing_order_is_404_and_audited(self):
        response = self.client.post("/orders/cancel", json={
            "order_id": "missing",
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "ORDER_NOT_FOUND")
        audit = self.service.get_audit()[-1]
        self.assertEqual(audit["action"], "cancel_order")
        self.assertEqual(audit["status"], 404)

    def test_nothing_removed_from_capability_profiles(self):
        standard = AmazonShopService.CAPABILITY_PROFILES["standard-v1"]
        full = AmazonShopService.CAPABILITY_PROFILES["full-v1"]
        for name in EXISTING_ACTIONS:
            self.assertIn(name, standard)
            self.assertIn(name, full)


if __name__ == "__main__":
    unittest.main()
