"""scheduled_price_watcher service — A narrative-clock-aware service for scheduling product-price watches and retrieving structured delivery observations.

SYNTHESIZED via services/synthesis.py (constrained CRUD codegen); spec at
services/_synthesis/scheduled_price_watcher.spec.json. Bespoke mechanics beyond CRUD, if
any, are listed in the spec's ``notes`` and are intentionally NOT implemented.

Fixture shape:
    price_watches [ {id: str, product_id: str, schedule: str, next_wakeup_at: str, active: bool} ]
    price_observations [ {id: str, watch_id: str, product_id: str, price: float, currency: str, observed_at: str, delivered_at: str, event_version: int, replay_count: int} ]
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class IdRequest(BaseModel):
    id: str


class UpdateRequest(BaseModel):
    id: str
    fields: dict = {}


class CreatePriceWatchRequest(BaseModel):
    product_id: str = ""
    schedule: str = ""
    next_wakeup_at: str = ""
    active: bool = False


class CreatePriceObservationRequest(BaseModel):
    watch_id: str = ""
    product_id: str = ""
    price: float = 0.0
    currency: str = ""
    observed_at: str = ""
    delivered_at: str = ""
    event_version: int = 0
    replay_count: int = 0


class ScheduledPriceWatcherService(ServiceBase):
    _READ = (
        "list_price_watches",
        "get_price_watch",
        "list_price_observations",
        "get_price_observation",
    )
    _MUTATE = ("create_price_watch", "update_price_watch")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_price_watches": action_contract(
            "core",
            output={"price_watches": "array"},
            audit=("action", "status"),
            tags=("scheduled_price_watcher", "read"),
        ),
        "get_price_watch": action_contract(
            "core",
            output={"price_watch": "object"},
            errors=(("PRICE_WATCH_NOT_FOUND", 404, "price watch id is absent"),),
            audit=("action", "status"),
            tags=("scheduled_price_watcher", "read"),
        ),
        "create_price_watch": action_contract(
            "core",
            output={"price_watch": "object"},
            effects=("append one price_watch record",),
            audit=("action", "status", "created_id"),
            tags=("scheduled_price_watcher", "create", "state_change"),
        ),
        "update_price_watch": action_contract(
            "core",
            output={"price_watch": "object"},
            effects=("shallow-merge fields into one existing price_watch",),
            errors=(("PRICE_WATCH_NOT_FOUND", 404, "price watch id is absent"),),
            audit=("action", "status"),
            tags=("scheduled_price_watcher", "update", "state_change"),
        ),
        "list_price_observations": action_contract(
            "core",
            output={"price_observations": "array"},
            audit=("action", "status"),
            tags=("scheduled_price_watcher", "read"),
        ),
        "get_price_observation": action_contract(
            "core",
            output={"price_observation": "object"},
            errors=(("PRICE_OBSERVATION_NOT_FOUND", 404, "price observation id is absent"),),
            audit=("action", "status"),
            tags=("scheduled_price_watcher", "read"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="scheduled_price_watcher")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "price_watches": [
            {
                "id": "str",
                "product_id": "str",
                "schedule": "str",
                "next_wakeup_at": "str",
                "active": "bool"
            }
        ],
        "price_observations": [
            {
                "id": "str",
                "watch_id": "str",
                "product_id": "str",
                "price": "float",
                "currency": "str",
                "observed_at": "str",
                "delivered_at": "str",
                "event_version": "int",
                "replay_count": "int"
            }
        ]
    }

    FIXTURE_TEMPLATE = {
        "migrated": False,
        "schema": FIXTURE_SCHEMA,
        "fields": {
            "price_watches[].id": {
                "source": "compiler",
                "meaning": "Stable price-watch identifier.",
                "format": "stable_id",
            },
            "price_watches[].product_id": {
                "source": "model",
                "meaning": "Catalog product identifier under watch.",
                "format": "plain_text",
            },
            "price_watches[].schedule": {
                "source": "model",
                "meaning": "Schedule descriptor for narrative-clock wakeups.",
                "format": "plain_text",
                "default": "narrative_clock",
            },
            "price_watches[].next_wakeup_at": {
                "source": "timeplan",
                "meaning": "Next scheduled wakeup timestamp.",
                "format": "iso8601",
            },
            "price_watches[].active": {
                "source": "model",
                "meaning": "Whether the watch is actively scheduling probes.",
                "format": "boolean",
                "default": True,
            },
            "price_observations[].id": {
                "source": "compiler",
                "meaning": "Stable observation identifier for deduplication.",
                "format": "stable_id",
            },
            "price_observations[].watch_id": {
                "source": "model",
                "meaning": "Parent price-watch identifier.",
                "format": "plain_text",
            },
            "price_observations[].product_id": {
                "source": "model",
                "meaning": "Product identifier sampled by the observation.",
                "format": "plain_text",
            },
            "price_observations[].price": {
                "source": "model",
                "meaning": "Observed price in minor currency units.",
            },
            "price_observations[].currency": {
                "source": "model",
                "meaning": "ISO currency code for the observed price.",
                "format": "plain_text",
                "default": "USD",
            },
            "price_observations[].observed_at": {
                "source": "timeplan",
                "meaning": "When the price was sampled.",
                "format": "iso8601",
            },
            "price_observations[].delivered_at": {
                "source": "timeplan",
                "meaning": "When the observation was delivered to consumers.",
                "format": "iso8601",
            },
            "price_observations[].event_version": {
                "source": "model",
                "meaning": "Version of the delivered event schema.",
                "default": 1,
            },
            "price_observations[].replay_count": {
                "source": "model",
                "meaning": "Number of times this observation was replayed.",
                "default": 0,
            },
        },
        "materialization_slots": {
            "price_observations": {
                "path": "price_observations",
                "material_kinds": ["price_observation", "price_observations"],
                "description": "Structured price observations including replay metadata.",
                "compiler_fields": ["price_observations[].id"],
                "cardinality": "scenario",
            },
            "price_watches": {
                "path": "price_watches",
                "material_kinds": ["price_watch", "price_watches"],
                "description": "Scheduled product price watches.",
                "compiler_fields": ["price_watches[].id"],
                "cardinality": "scenario",
            },
        },
    }

    FIXTURE_SLOTS = {
        "price_observations": {
            "path": "price_observations",
            "material_kinds": ["price_observation", "price_observations"],
            "description": "Structured price observations including replay metadata.",
            "cardinality": "scenario",
        },
        "price_watches": {
            "path": "price_watches",
            "material_kinds": ["price_watch", "price_watches"],
            "description": "Scheduled product price watches.",
            "cardinality": "scenario",
        },
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.price_watches = list(data.get("price_watches", []))
        self.price_observations = list(data.get("price_observations", []))

    def get_state(self) -> dict:
        return {"price_watches": self.price_watches, "price_observations": self.price_observations}

    def inject(self, content: dict) -> dict:
        """Environment-side injection: append records to a named collection."""
        collection = content.get("collection", "price_watches")
        records = content.get("records", [])
        target = getattr(self, collection, None)
        if target is None or not isinstance(records, list):
            return {"error": f"unknown collection {collection!r}"}
        target.extend(records)
        return {"injected": len(records), "collection": collection}

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/price_watches/list", operation_id="list_price_watches",
                          summary="List all price_watches")
        def list_price_watches() -> dict:
            svc.log_action("list_price_watches", params={}, count=len(svc.price_watches))
            return {"price_watches": svc.price_watches}

        @self.router.post("/price_watches/get", operation_id="get_price_watch",
                          summary="Get one price_watch by id")
        def get_price_watch(req: IdRequest):
            svc.log_action("get_price_watch", params={"id": req.id})
            for item in svc.price_watches:
                if str(item.get("id")) == str(req.id):
                    return {"price_watch": item}
            return JSONResponse(status_code=404,
                                content={"error": "price_watch not found"})

        @self.router.post("/price_watches/create", operation_id="create_price_watch",
                          summary="Create a new price_watch")
        def create_price_watch(req: CreatePriceWatchRequest):
            item = req.model_dump()
            item["id"] = f"price_watch-{uuid.uuid4().hex[:8]}"
            svc.price_watches.append(item)
            svc.log_action("create_price_watch", params=req.model_dump(),
                           created_id=item["id"])
            return {"price_watch": item}

        @self.router.post("/price_watches/update", operation_id="update_price_watch",
                          summary="Update fields of an existing price_watch")
        def update_price_watch(req: UpdateRequest):
            svc.log_action("update_price_watch",
                           params={"id": req.id, "fields": req.fields})
            for item in svc.price_watches:
                if str(item.get("id")) == str(req.id):
                    item.update({k: v for k, v in req.fields.items()
                                 if k != "id"})
                    return {"price_watch": item}
            return JSONResponse(status_code=404,
                                content={"error": "price_watch not found"})

        @self.router.post("/price_observations/list", operation_id="list_price_observations",
                          summary="List all price_observations")
        def list_price_observations() -> dict:
            svc.log_action("list_price_observations", params={}, count=len(svc.price_observations))
            return {"price_observations": svc.price_observations}

        @self.router.post("/price_observations/get", operation_id="get_price_observation",
                          summary="Get one price_observation by id")
        def get_price_observation(req: IdRequest):
            svc.log_action("get_price_observation", params={"id": req.id})
            for item in svc.price_observations:
                if str(item.get("id")) == str(req.id):
                    return {"price_observation": item}
            return JSONResponse(status_code=404,
                                content={"error": "price_observation not found"})


def create_service(service_id: str) -> ScheduledPriceWatcherService:
    svc = ScheduledPriceWatcherService(service_id)
    svc.load_data()
    return svc
