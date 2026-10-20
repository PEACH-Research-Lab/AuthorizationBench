"""Smoke tests for services/phone/service.py."""

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

from services.phone.service import PhoneService, create_service  # noqa: E402


FIXTURE = {
    "owner_name": "Alex Morgan",
    "owner_phone": "+1-555-0100",
    "contacts": [
        {
            "id": "ct-1",
            "first_name": "Sam",
            "last_name": "Rivera",
            "phone_number": "+1-555-0101",
            "email": "sam@example.com",
            "relationship": "friend",
        },
        {
            "id": "ct-2",
            "first_name": "Jordan",
            "last_name": "Lee",
            "phone_number": "+1-555-0102",
            "email": "jordan@example.com",
            "relationship": "colleague",
        },
    ],
    "calls": [
        {
            "id": "call-0",
            "contact_id": "ct-2",
            "phone_number": "+1-555-0102",
            "contact_name": "Jordan Lee",
            "direction": "incoming",
            "duration_seconds": 45,
            "timestamp": "2025-01-10T09:00:00",
        }
    ],
}


class PhoneServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["PHONE_FIXTURES"] = self._tmp.name
        self.svc = create_service("phone")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/phone")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("PHONE_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "list_contacts",
            "get_contact",
            "list_calls",
            "call_contact",
        }
        self.assertEqual(set(PhoneService.CAPABILITY_PROFILES["standard-v1"]), expected)
        self.assertEqual(set(PhoneService.CAPABILITY_PROFILES["full-v1"]), expected)
        self.assertEqual(set(PhoneService.ACTION_CONTRACTS), expected)
        for name in PhoneService._READ:
            contract = PhoneService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in PhoneService._MUTATE:
            contract = PhoneService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_list_get_contacts_and_calls(self) -> None:
        r = self.client.post("/phone/contacts/list")
        self.assertEqual(r.status_code, 200)
        ids = [c["id"] for c in r.json()["contacts"]]
        self.assertEqual(ids, ["ct-2", "ct-1"])

        r = self.client.post("/phone/contacts/get", json={"contact_id": "ct-1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["contact"]["phone_number"], "+1-555-0101")

        r = self.client.post("/phone/calls/list")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["calls"]), 1)
        self.assertEqual(r.json()["calls"][0]["id"], "call-0")

    def test_call_contact_records_outgoing(self) -> None:
        r = self.client.post(
            "/phone/contacts/call",
            json={"contact_id": "ct-1", "duration_seconds": 30},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["call"]["direction"], "outgoing")
        self.assertEqual(body["call"]["contact_id"], "ct-1")
        self.assertEqual(body["call"]["duration_seconds"], 30)
        call_id = body["call"]["id"]

        state = self.client.get("/phone/state").json()["state"]
        self.assertEqual(len(state["calls"]), 2)
        self.assertEqual(state["calls"][-1]["id"], call_id)

        audit = self.client.get("/phone/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "call_contact")
        self.assertEqual(audit[-1]["status"], 200)

    def test_missing_contact_is_logged(self) -> None:
        r = self.client.post(
            "/phone/contacts/get",
            json={"contact_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "CONTACT_NOT_FOUND")

        r = self.client.post(
            "/phone/contacts/call",
            json={"contact_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        audit = self.client.get("/phone/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "call_contact")
        self.assertEqual(audit[-1]["status"], 404)


if __name__ == "__main__":
    unittest.main()
