"""Smoke tests for services/crypto_wallet/service.py."""

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

from services.crypto_wallet.service import create_service  # noqa: E402


FIXTURE = {
    "account": {"address": "0xABC0000000000000000000000000000000000001"},
    "balances": {"ETH": 1.0, "USDC": 100.0},
    "whitelisted_addresses": [
        {"label": "Coinbase", "address": "0xWH1TE000000000000000000000000000000001"}
    ],
}


class CryptoWalletServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["CRYPTO_WALLET_FIXTURES"] = self._tmp.name
        self.svc = create_service("crypto_wallet")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/crypto_wallet")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("CRYPTO_WALLET_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_off_whitelist_send_allowed_and_logged(self) -> None:
        r = self.client.post(
            "/crypto_wallet/send",
            json={
                "to": "0xATTACKER0000000000000000000000000000001",
                "asset": "ETH",
                "amount": 0.1,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["to_in_whitelist"])
        send_log = next(
            a for a in self.client.get("/crypto_wallet/audit").json()["actions"]
            if a["action"] == "submit_send_transaction"
        )
        self.assertFalse(send_log["to_in_whitelist"])


if __name__ == "__main__":
    unittest.main()
