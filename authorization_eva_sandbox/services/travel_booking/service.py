"""travel_booking service — fixture-backed flight, hotel, and car search/reserve.

Reduced from AgentDojo travel suite + LifeOps BOOK_TRAVEL: deterministic
offer search and reservation only. No restaurants, no live network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class FlightSearchRequest(BaseModel):
    departure_city: str = ""
    arrival_city: str = ""


class HotelSearchRequest(BaseModel):
    city: str = ""


class CarSearchRequest(BaseModel):
    city: str = ""


class BookFlightRequest(BaseModel):
    offer_id: str
    passenger_name: str = ""


class BookHotelRequest(BaseModel):
    offer_id: str
    check_in: str = Field("", description="ISO date YYYY-MM-DD")
    check_out: str = Field("", description="ISO date YYYY-MM-DD")


class BookCarRequest(BaseModel):
    offer_id: str
    start_time: str = Field("", description="ISO datetime")
    end_time: str = Field("", description="ISO datetime")


class CancelReservationRequest(BaseModel):
    reservation_id: str


class TravelBookingService(ServiceBase):
    _READ = ("search_flights", "search_hotels", "search_cars")
    _WRITE = ("book_flight", "book_hotel", "book_car", "cancel_reservation")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _WRITE,
        "full-v1": _READ + _WRITE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean", "offers": "array"},
            audit=("action", "status"),
            tags=("travel", "search", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS.update({
        "book_flight": action_contract(
            "core", output={"ok": "boolean", "reservation": "object"},
            effects=("create one confirmed flight reservation",),
            errors=(("OFFER_NOT_FOUND", 404, "flight offer id is absent"),),
            audit=("action", "status", "reservation_id", "offer_id"),
            tags=("travel", "flight", "book", "state_change"),
        ),
        "book_hotel": action_contract(
            "core", output={"ok": "boolean", "reservation": "object"},
            effects=("create one confirmed hotel reservation",),
            errors=(("OFFER_NOT_FOUND", 404, "hotel offer id is absent"),),
            audit=("action", "status", "reservation_id", "offer_id"),
            tags=("travel", "hotel", "book", "state_change"),
        ),
        "book_car": action_contract(
            "core", output={"ok": "boolean", "reservation": "object"},
            effects=("create one confirmed car reservation",),
            errors=(("OFFER_NOT_FOUND", 404, "car offer id is absent"),),
            audit=("action", "status", "reservation_id", "offer_id"),
            tags=("travel", "car", "book", "state_change"),
        ),
        "cancel_reservation": action_contract(
            "core", output={"ok": "boolean", "reservation": "object"},
            preconditions=("reservation exists and is confirmed",),
            effects=("set reservation status to cancelled",),
            errors=(
                ("RESERVATION_NOT_FOUND", 404, "reservation id is absent"),
                ("ALREADY_CANCELLED", 409, "reservation already cancelled"),
            ),
            audit=("action", "status", "reservation_id"),
            tags=("travel", "reservation", "cancel", "state_change"),
        ),
    })

    def __init__(self, service_id: str):
        super().__init__(service_id, template="travel_booking")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "traveler_name": "str",
        "traveler_email": "str",
        "traveler_phone": "str",
        "flights": [{
            "id": "str",
            "airline": "str",
            "flight_number": "str",
            "departure_city": "str",
            "arrival_city": "str",
            "departure_time": "str",
            "arrival_time": "str",
            "price": "float",
            "contact_information": "str",
        }],
        "hotels": [{
            "id": "str",
            "name": "str",
            "city": "str",
            "rating": "float",
            "price_min": "float",
            "price_max": "float",
            "address": "str",
            "reviews": "list",
        }],
        "cars": [{
            "id": "str",
            "name": "str",
            "city": "str",
            "address": "str",
            "rating": "float",
            "price_per_day": "float",
            "reviews": "list",
            "contact_information": "str",
            "car_types_available": "list",
            "fuel_options": "list",
        }],
        "reservations": [{
            "reservation_id": "str",
            "reservation_type": "enum:flight|hotel|car",
            "offer_id": "str",
            "title": "str",
            "start_time": "str",
            "end_time": "str",
            "contact_information": "str",
            "status": "enum:confirmed|cancelled",
            "price": "float",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.traveler_name = data.get("traveler_name", "")
        self.traveler_email = data.get("traveler_email", "")
        self.traveler_phone = data.get("traveler_phone", "")
        self.flights = list(data.get("flights", []))
        self.hotels = list(data.get("hotels", []))
        self.cars = list(data.get("cars", []))
        self.reservations = list(data.get("reservations", []))
        for r in self.reservations:
            r.setdefault("status", "confirmed")

    def get_state(self) -> dict:
        return {
            "traveler_name": self.traveler_name,
            "traveler_email": self.traveler_email,
            "traveler_phone": self.traveler_phone,
            "flights": self.flights,
            "hotels": self.hotels,
            "cars": self.cars,
            "reservations": self.reservations,
        }

    def _find_offer(self, collection: list[dict], offer_id: str) -> Optional[dict]:
        return next(
            (item for item in collection if str(item.get("id")) == str(offer_id)),
            None,
        )

    def _find_reservation(self, reservation_id: str) -> Optional[dict]:
        return next(
            (
                r for r in self.reservations
                if str(r.get("reservation_id")) == str(reservation_id)
            ),
            None,
        )

    @staticmethod
    def _city_match(value: str, query: str) -> bool:
        q = (query or "").strip().lower()
        if not q:
            return True
        return q in str(value or "").strip().lower()

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/flights/search", operation_id="search_flights",
                          summary="Search fixture flight offers by route")
        def search_flights(req: FlightSearchRequest) -> dict:
            offers = [
                f for f in svc.flights
                if svc._city_match(f.get("departure_city", ""), req.departure_city)
                and svc._city_match(f.get("arrival_city", ""), req.arrival_city)
            ]
            svc.log_action(
                "search_flights",
                params=req.model_dump(),
                offer_count=len(offers),
            )
            return {"ok": True, "offers": offers}

        @self.router.post("/hotels/search", operation_id="search_hotels",
                          summary="Search fixture hotel offers by city")
        def search_hotels(req: HotelSearchRequest) -> dict:
            offers = [
                h for h in svc.hotels
                if svc._city_match(h.get("city", ""), req.city)
            ]
            svc.log_action(
                "search_hotels",
                params=req.model_dump(),
                offer_count=len(offers),
            )
            return {"ok": True, "offers": offers}

        @self.router.post("/cars/search", operation_id="search_cars",
                          summary="Search fixture car rental offers by city")
        def search_cars(req: CarSearchRequest) -> dict:
            offers = [
                c for c in svc.cars
                if svc._city_match(c.get("city", ""), req.city)
            ]
            svc.log_action(
                "search_cars",
                params=req.model_dump(),
                offer_count=len(offers),
            )
            return {"ok": True, "offers": offers}

        @self.router.post("/flights/book", operation_id="book_flight",
                          summary="Book a fixture flight offer")
        def book_flight(req: BookFlightRequest):
            params = req.model_dump()
            offer = svc._find_offer(svc.flights, req.offer_id)
            if not offer:
                svc.log_action(
                    "book_flight", params=params, status=404,
                    offer_id=req.offer_id,
                )
                return svc.error_response(
                    code="OFFER_NOT_FOUND",
                    message="flight offer not found",
                    status=404,
                    details={"offer_id": req.offer_id},
                )
            reservation = {
                "reservation_id": svc.new_id("reservation"),
                "reservation_type": "flight",
                "offer_id": offer["id"],
                "title": f"{offer.get('airline', '')} {offer.get('flight_number', '')}".strip(),
                "start_time": offer.get("departure_time", ""),
                "end_time": offer.get("arrival_time", ""),
                "contact_information": (
                    req.passenger_name
                    or svc.traveler_phone
                    or svc.traveler_name
                ),
                "status": "confirmed",
                "price": float(offer.get("price", 0.0) or 0.0),
            }
            svc.reservations.append(reservation)
            svc.log_action(
                "book_flight",
                params=params,
                reservation_id=reservation["reservation_id"],
                offer_id=req.offer_id,
            )
            return {"ok": True, "reservation": reservation}

        @self.router.post("/hotels/book", operation_id="book_hotel",
                          summary="Book a fixture hotel offer")
        def book_hotel(req: BookHotelRequest):
            params = req.model_dump()
            offer = svc._find_offer(svc.hotels, req.offer_id)
            if not offer:
                svc.log_action(
                    "book_hotel", params=params, status=404,
                    offer_id=req.offer_id,
                )
                return svc.error_response(
                    code="OFFER_NOT_FOUND",
                    message="hotel offer not found",
                    status=404,
                    details={"offer_id": req.offer_id},
                )
            reservation = {
                "reservation_id": svc.new_id("reservation"),
                "reservation_type": "hotel",
                "offer_id": offer["id"],
                "title": offer.get("name", ""),
                "start_time": req.check_in,
                "end_time": req.check_out,
                "contact_information": svc.traveler_phone or svc.traveler_name,
                "status": "confirmed",
                "price": float(offer.get("price_min", 0.0) or 0.0),
            }
            svc.reservations.append(reservation)
            svc.log_action(
                "book_hotel",
                params=params,
                reservation_id=reservation["reservation_id"],
                offer_id=req.offer_id,
            )
            return {"ok": True, "reservation": reservation}

        @self.router.post("/cars/book", operation_id="book_car",
                          summary="Book a fixture car rental offer")
        def book_car(req: BookCarRequest):
            params = req.model_dump()
            offer = svc._find_offer(svc.cars, req.offer_id)
            if not offer:
                svc.log_action(
                    "book_car", params=params, status=404,
                    offer_id=req.offer_id,
                )
                return svc.error_response(
                    code="OFFER_NOT_FOUND",
                    message="car offer not found",
                    status=404,
                    details={"offer_id": req.offer_id},
                )
            reservation = {
                "reservation_id": svc.new_id("reservation"),
                "reservation_type": "car",
                "offer_id": offer["id"],
                "title": offer.get("name", ""),
                "start_time": req.start_time,
                "end_time": req.end_time,
                "contact_information": (
                    offer.get("contact_information")
                    or svc.traveler_phone
                    or svc.traveler_name
                ),
                "status": "confirmed",
                "price": float(offer.get("price_per_day", 0.0) or 0.0),
            }
            svc.reservations.append(reservation)
            svc.log_action(
                "book_car",
                params=params,
                reservation_id=reservation["reservation_id"],
                offer_id=req.offer_id,
            )
            return {"ok": True, "reservation": reservation}

        @self.router.post("/reservations/cancel", operation_id="cancel_reservation",
                          summary="Cancel a confirmed reservation")
        def cancel_reservation(req: CancelReservationRequest):
            params = req.model_dump()
            reservation = svc._find_reservation(req.reservation_id)
            if not reservation:
                svc.log_action(
                    "cancel_reservation", params=params, status=404,
                    reservation_id=req.reservation_id,
                )
                return svc.error_response(
                    code="RESERVATION_NOT_FOUND",
                    message="reservation not found",
                    status=404,
                    details={"reservation_id": req.reservation_id},
                )
            if reservation.get("status") == "cancelled":
                svc.log_action(
                    "cancel_reservation", params=params, status=409,
                    reservation_id=req.reservation_id, no_op=True,
                )
                return svc.error_response(
                    code="ALREADY_CANCELLED",
                    message="reservation already cancelled",
                    status=409,
                    details={"reservation_id": req.reservation_id},
                )
            reservation["status"] = "cancelled"
            svc.log_action(
                "cancel_reservation",
                params=params,
                reservation_id=req.reservation_id,
            )
            return {"ok": True, "reservation": reservation}


def create_service(service_id: str) -> TravelBookingService:
    return TravelBookingService(service_id)
