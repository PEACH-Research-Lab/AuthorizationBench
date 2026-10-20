"""comparison_shopping service — A generic cross-store product search surface for browsing and inspecting comparison-shopping listings.

SYNTHESIZED via services/synthesis.py (constrained CRUD codegen); spec at
services/_synthesis/comparison_shopping.spec.json. Bespoke mechanics beyond CRUD, if
any, are listed in the spec's ``notes`` and are intentionally NOT implemented.

Fixture shape:
    listings [ {id: str, title: str, seller_store: str, price: float, currency: str, rating: float, sponsored: bool} ]
"""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class IdRequest(BaseModel):
    id: str


class UpdateRequest(BaseModel):
    id: str
    fields: dict = {}


class CreateListingRequest(BaseModel):
    title: str = ""
    seller_store: str = ""
    price: float = 0.0
    currency: str = ""
    rating: float = 0.0
    sponsored: bool = False


class ComparisonShoppingService(ServiceBase):
    _READ = ("list_listings", "get_listing")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ,
        "full-v1": _READ,
    }
    ACTION_CONTRACTS = {
        "list_listings": action_contract(
            "core", output={"listings": "array"},
            audit=("action", "status"),
            tags=("comparison_shopping", "read"),
        ),
        "get_listing": action_contract(
            "core", output={"listing": "object"},
            errors=(("LISTING_NOT_FOUND", 404, "listing id is absent"),),
            audit=("action", "status"),
            tags=("comparison_shopping", "read"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="comparison_shopping")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "listings": [{"id": "str", "title": "str", "seller_store": "str",
                      "price": "float", "currency": "str", "rating": "float",
                      "sponsored": "bool"}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.listings = list(data.get("listings", []))

    def get_state(self) -> dict:
        return {"listings": self.listings}

    def inject(self, content: dict) -> dict:
        """Environment-side injection: append records to a named collection."""
        collection = content.get("collection", "listings")
        records = content.get("records", [])
        target = getattr(self, collection, None)
        if target is None or not isinstance(records, list):
            return {"error": f"unknown collection {collection!r}"}
        target.extend(records)
        return {"injected": len(records), "collection": collection}

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/listings/list", operation_id="list_listings",
                          summary="List all listings")
        def list_listings() -> dict:
            svc.log_action("list_listings", params={}, count=len(svc.listings))
            return {"listings": svc.listings}

        @self.router.post("/listings/get", operation_id="get_listing",
                          summary="Get one listing by id")
        def get_listing(req: IdRequest):
            params = {"id": req.id}
            for item in svc.listings:
                if str(item.get("id")) == str(req.id):
                    svc.log_action("get_listing", params=params)
                    return {"listing": item}
            svc.log_action("get_listing", params=params, status=404)
            return svc.error_response(
                code="LISTING_NOT_FOUND",
                message="listing not found",
                status=404,
                details={"id": req.id},
            )


def create_service(service_id: str) -> ComparisonShoppingService:
    svc = ComparisonShoppingService(service_id)
    svc.load_data()
    return svc
