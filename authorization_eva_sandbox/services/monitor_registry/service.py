"""monitor_registry service — A service for creating, listing, inspecting, and updating generic polling monitors and their histories.

SYNTHESIZED via services/synthesis.py (constrained CRUD codegen); spec at
services/_synthesis/monitor_registry.spec.json. Bespoke mechanics beyond CRUD, if
any, are listed in the spec's ``notes`` and are intentionally NOT implemented.

Fixture shape:
    monitors [ {id: str, name: str, target_type: str, watched_target: str, condition: dict, polling_state: str, poll_history: list} ]
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


class CreateMonitorRequest(BaseModel):
    name: str = ""
    target_type: str = ""
    watched_target: str = ""
    condition: dict = {}
    polling_state: str = ""
    poll_history: list = []


class MonitorRegistryService(ServiceBase):
    _READ = ("list_monitors", "get_monitor")
    _MUTATE = ("create_monitor", "update_monitor")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_monitors": action_contract(
            "core", output={"monitors": "array"},
            audit=("action", "status"),
            tags=("monitor_registry", "read"),
        ),
        "get_monitor": action_contract(
            "core", output={"monitor": "object"},
            errors=(("MONITOR_NOT_FOUND", 404, "monitor id is absent"),),
            audit=("action", "status"),
            tags=("monitor_registry", "read"),
        ),
        "create_monitor": action_contract(
            "core", output={"monitor": "object"},
            effects=("append one monitor record",),
            audit=("action", "status", "created_id"),
            tags=("monitor_registry", "create", "state_change"),
        ),
        "update_monitor": action_contract(
            "core", output={"monitor": "object"},
            effects=("shallow-merge fields into one existing monitor",),
            errors=(("MONITOR_NOT_FOUND", 404, "monitor id is absent"),),
            audit=("action", "status"),
            tags=("monitor_registry", "update", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="monitor_registry")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "monitors": [
            {
                "id": "str",
                "name": "str",
                "target_type": "str",
                "watched_target": "str",
                "condition": "dict",
                "polling_state": "str",
                "poll_history": "list"
            }
        ]
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.monitors = list(data.get("monitors", []))

    def get_state(self) -> dict:
        return {"monitors": self.monitors}

    def inject(self, content: dict) -> dict:
        """Environment-side injection: append records to a named collection."""
        collection = content.get("collection", "monitors")
        records = content.get("records", [])
        target = getattr(self, collection, None)
        if target is None or not isinstance(records, list):
            return {"error": f"unknown collection {collection!r}"}
        target.extend(records)
        return {"injected": len(records), "collection": collection}

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/monitors/list", operation_id="list_monitors",
                          summary="List all monitors")
        def list_monitors() -> dict:
            svc.log_action("list_monitors", params={}, count=len(svc.monitors))
            return {"monitors": svc.monitors}

        @self.router.post("/monitors/get", operation_id="get_monitor",
                          summary="Get one monitor by id")
        def get_monitor(req: IdRequest):
            params = {"id": req.id}
            for item in svc.monitors:
                if str(item.get("id")) == str(req.id):
                    svc.log_action("get_monitor", params=params)
                    return {"monitor": item}
            svc.log_action("get_monitor", params=params, status=404)
            return svc.error_response(
                code="MONITOR_NOT_FOUND",
                message="monitor not found",
                status=404,
                details={"id": req.id},
            )

        @self.router.post("/monitors/create", operation_id="create_monitor",
                          summary="Create a new monitor")
        def create_monitor(req: CreateMonitorRequest):
            item = req.model_dump()
            item["id"] = svc.new_id("monitor")
            svc.monitors.append(item)
            svc.log_action("create_monitor", params=req.model_dump(),
                           created_id=item["id"])
            return {"monitor": item}

        @self.router.post("/monitors/update", operation_id="update_monitor",
                          summary="Update fields of an existing monitor")
        def update_monitor(req: UpdateRequest):
            params = {"id": req.id, "fields": req.fields}
            for item in svc.monitors:
                if str(item.get("id")) == str(req.id):
                    item.update({k: v for k, v in req.fields.items()
                                 if k != "id"})
                    svc.log_action("update_monitor", params=params)
                    return {"monitor": item}
            svc.log_action("update_monitor", params=params, status=404)
            return svc.error_response(
                code="MONITOR_NOT_FOUND",
                message="monitor not found",
                status=404,
                details={"id": req.id},
            )


def create_service(service_id: str) -> MonitorRegistryService:
    svc = MonitorRegistryService(service_id)
    svc.load_data()
    return svc
