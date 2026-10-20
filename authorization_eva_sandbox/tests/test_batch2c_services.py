"""Smoke tests for batch-2c service migrations."""

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

from services.registry import derive_actions  # noqa: E402


class Batch2cServicesTest(unittest.TestCase):
  def test_etrade_broker_order_flow(self) -> None:
    fixture = {
      "securities": [
        {"symbol": "AAPL", "company_name": "Apple", "last_price": 100.0, "top_match": True}
      ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
      json.dump(fixture, tmp)
      path = tmp.name
    os.environ["ETRADE_BROKER_FIXTURES"] = path
    try:
      from services.etrade_broker.service import create_service

      svc = create_service("etrade_broker")
      svc.load_data()
      app = FastAPI()
      app.include_router(svc.router, prefix="/etrade_broker")
      client = TestClient(app)
      client.post("/etrade_broker/search", json={"query": "AAPL"})
      prev = client.post(
        "/etrade_broker/order/preview",
        json={"symbol": "AAPL", "quantity": 1, "side": "buy"},
      ).json()
      token = prev["preview_token"]
      client.post("/etrade_broker/order/place", json={"preview_token": token})
      actions = [a["action"] for a in client.get("/etrade_broker/audit").json()["actions"]]
      self.assertIn("place_order", actions)
    finally:
      os.environ.pop("ETRADE_BROKER_FIXTURES", None)
      Path(path).unlink(missing_ok=True)

  def test_derived_actions_nonempty(self) -> None:
    for template in (
      "dev_forum",
      "school_directory",
      "sales_analysis_site",
      "wechat_app",
      "thomasnet_marketplace",
      "quickgist_ai",
      "github_repo",
      "etrade_broker",
    ):
      actions = derive_actions(template)
      self.assertGreater(len(actions), 0, template)


if __name__ == "__main__":
  unittest.main()
