"""Smoke tests for services/travel_booking/service.py."""

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

from services.travel_booking.service import TravelBookingService, create_service  # noqa: E402


FIXTURE = {
    "traveler_name": "Alex Morgan",
    "traveler_email": "alex@example.com",
    "traveler_phone": "+1-555-0100",
    "flights": [
        {
            "id": "flt-1",
            "airline": "SkyLine",
            "flight_number": "SL100",
            "departure_city": "Paris",
            "arrival_city": "London",
            "departure_time": "2025-01-11T09:00:00",
            "arrival_time": "2025-01-11T10:15:00",
            "price": 120.0,
            "contact_information": "flights@skyline.example",
        },
        {
            "id": "flt-2",
            "airline": "SkyLine",
            "flight_number": "SL200",
            "departure_city": "Tokyo",
            "arrival_city": "Osaka",
            "departure_time": "2025-02-01T08:00:00",
            "arrival_time": "2025-02-01T09:30:00",
            "price": 90.0,
            "contact_information": "flights@skyline.example",
        },
    ],
    "hotels": [
        {
            "id": "htl-1",
            "name": "Le Marais Boutique",
            "city": "Paris",
            "rating": 4.5,
            "price_min": 180.0,
            "price_max": 320.0,
            "address": "12 Rue du Temple, Paris",
            "reviews": ["Great location"],
        }
    ],
    "cars": [
        {
            "id": "car-1",
            "name": "Paris Drive",
            "city": "Paris",
            "address": "1 Airport Rd, Paris",
            "rating": 4.2,
            "price_per_day": 55.0,
            "reviews": ["Clean cars"],
            "contact_information": "rentals@parisdrive.example",
            "car_types_available": ["Sedan", "SUV"],
            "fuel_options": ["Regular", "Electric"],
        }
    ],
    "reservations": [],
}


class TravelBookingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["TRAVEL_BOOKING_FIXTURES"] = self._tmp.name
        self.svc = create_service("travel_booking")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/travel_booking")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("TRAVEL_BOOKING_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "search_flights",
            "search_hotels",
            "search_cars",
            "book_flight",
            "book_hotel",
            "book_car",
            "cancel_reservation",
        }
        self.assertEqual(set(TravelBookingService.CAPABILITY_PROFILES["standard-v1"]), expected)
        self.assertEqual(set(TravelBookingService.CAPABILITY_PROFILES["full-v1"]), expected)
        self.assertEqual(set(TravelBookingService.ACTION_CONTRACTS), expected)
        for name in TravelBookingService._READ:
            contract = TravelBookingService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in TravelBookingService._WRITE:
            contract = TravelBookingService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_search_book_cancel_flight(self) -> None:
        r = self.client.post(
            "/travel_booking/flights/search",
            json={"departure_city": "Paris", "arrival_city": "London"},
        )
        self.assertEqual(r.status_code, 200)
        offers = r.json()["offers"]
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["id"], "flt-1")

        r = self.client.post(
            "/travel_booking/flights/book",
            json={"offer_id": "flt-1", "passenger_name": "Alex Morgan"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        reservation_id = body["reservation"]["reservation_id"]
        self.assertEqual(body["reservation"]["status"], "confirmed")
        self.assertEqual(body["reservation"]["reservation_type"], "flight")

        r = self.client.post(
            "/travel_booking/reservations/cancel",
            json={"reservation_id": reservation_id},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reservation"]["status"], "cancelled")

        state = self.client.get("/travel_booking/state").json()["state"]
        self.assertEqual(len(state["reservations"]), 1)
        self.assertEqual(state["reservations"][0]["status"], "cancelled")

        audit = self.client.get("/travel_booking/audit").json()["actions"]
        self.assertEqual(
            [e["action"] for e in audit],
            ["search_flights", "book_flight", "cancel_reservation"],
        )

    def test_book_missing_offer_is_logged(self) -> None:
        r = self.client.post(
            "/travel_booking/hotels/book",
            json={"offer_id": "missing", "check_in": "2025-01-11", "check_out": "2025-01-15"},
        )
        self.assertEqual(r.status_code, 404)
        audit = self.client.get("/travel_booking/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "book_hotel")
        self.assertEqual(audit[-1]["status"], 404)


if __name__ == "__main__":
    unittest.main()
