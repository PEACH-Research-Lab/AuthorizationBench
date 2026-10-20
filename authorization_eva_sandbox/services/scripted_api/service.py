"""scripted_api service — A fixture-backed catalog of HTTP-like endpoints that agents can discover and query for predefined responses.

Originally SYNTHESIZED via services/synthesis.py (constrained CRUD codegen);
spec at services/_synthesis/scripted_api.spec.json. Hand-extended since with
ONE bespoke mechanic:

Time-versioned responses (narrative clock — services/_clock.py):
    An endpoint may carry ``response_variants: [{effective_from: ISO,
    response_body: dict, status_code?: int, notes?: str}]``. When the
    narrative clock is at or past a variant's ``effective_from`` the endpoint
    is served with that variant's body/status (latest effective wins); before
    the first ``effective_from`` the base ``response_body`` applies. This is
    how "the live pricing feed flips the promotion to active at 16:00" is
    modelled without any absolute run date: the timestamps are derived from
    task_config.reference_time and the clock is moved by environment events.

Fixture shape:
    endpoints [ {id: str, path: str, method: str, response_body: dict, status_code: int,
                 lifecycle_state: str, notes: str,
                 response_variants?: [{effective_from: str, response_body: dict,
                                       status_code: int, notes: str}]} ]
"""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services import _clock
from services.contracts import action_contract


class IdRequest(BaseModel):
    id: str


class UpdateRequest(BaseModel):
    id: str
    fields: dict = {}


class CreateEndpointRequest(BaseModel):
    path: str = ""
    method: str = ""
    response_body: dict = {}
    status_code: int = 0
    lifecycle_state: str = ""
    notes: str = ""


class ScriptedApiService(ServiceBase):
    _READ = ("list_endpoints", "get_endpoint")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ,
        "full-v1": _READ,
    }
    ACTION_CONTRACTS = {
        "list_endpoints": action_contract(
            "core", output={"endpoints": "array"},
            audit=("action", "status"),
            tags=("scripted_api", "read"),
        ),
        "get_endpoint": action_contract(
            "core", output={"endpoint": "object"},
            errors=(("ENDPOINT_NOT_FOUND", 404, "endpoint id is absent"),),
            audit=("action", "status"),
            tags=("scripted_api", "read"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="scripted_api")
        self._register_routes()

    # Read by Stage 4 prompts / the timeline compiler (see amazon_shop).
    CLOCK_SEMANTICS = (
        "endpoints[].response_variants[] {effective_from, response_body, "
        "status_code?, notes?}: once now >= effective_from the endpoint is "
        "served with that variant (latest effective wins). Use this for any "
        "response that must change at a scheduled instant (a feed flipping a "
        "promotion to active, a price update, an endpoint being retired)."
    )

    FIXTURE_SCHEMA = {
        "endpoints": [
            {
                "id": "str",
                "path": "str",
                "method": "str",
                "response_body": "dict",
                "status_code": "int",
                "lifecycle_state": "str",
                "notes": "str",
                # time-versioned overrides, judged against the narrative
                # clock; latest variant with effective_from <= now wins.
                "response_variants": [{"effective_from": "str",
                                       "response_body": "dict",
                                       "status_code": "int",
                                       "notes": "str"}],
            }
        ]
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.endpoints = list(data.get("endpoints", []))

    def get_state(self) -> dict:
        # Full-exposure convention: raw fixture (variants included) for the
        # evaluator, plus what the agent would currently be served.
        return {"endpoints": self.endpoints,
                "effective_endpoints": [self._effective(e) for e in self.endpoints],
                "narrative_time": _clock.now_iso()}

    def inject(self, content: dict) -> dict:
        """Environment-side injection: append records to a named collection."""
        collection = content.get("collection", "endpoints")
        records = content.get("records", [])
        target = getattr(self, collection, None)
        if target is None or not isinstance(records, list):
            return {"error": f"unknown collection {collection!r}"}
        target.extend(records)
        return {"injected": len(records), "collection": collection}

    # ── time-versioned rendering ─────────────────────────────────────────────

    def _effective(self, endpoint: dict) -> dict:
        """The endpoint as the world serves it NOW (variants applied, hidden)."""
        variants = endpoint.get("response_variants") or []
        if not variants:
            return endpoint
        now = _clock.now()
        chosen = None
        chosen_at = None
        for v in variants:
            if not isinstance(v, dict) or not v.get("effective_from"):
                continue
            try:
                at = _clock.parse_iso(v["effective_from"])
            except ValueError:
                continue
            if at <= now and (chosen_at is None or at >= chosen_at):
                chosen, chosen_at = v, at
        rendered = {k: v for k, v in endpoint.items() if k != "response_variants"}
        if chosen is not None:
            if "response_body" in chosen:
                rendered["response_body"] = chosen["response_body"]
            if chosen.get("status_code"):
                rendered["status_code"] = chosen["status_code"]
            if chosen.get("notes"):
                rendered["notes"] = chosen["notes"]
        return rendered

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/endpoints/list", operation_id="list_endpoints",
                          summary="List all endpoints")
        def list_endpoints() -> dict:
            svc.log_action("list_endpoints", params={}, count=len(svc.endpoints))
            return {"endpoints": [svc._effective(e) for e in svc.endpoints]}

        @self.router.post("/endpoints/get", operation_id="get_endpoint",
                          summary="Get one endpoint by id")
        def get_endpoint(req: IdRequest):
            params = {"id": req.id}
            for item in svc.endpoints:
                if str(item.get("id")) == str(req.id):
                    svc.log_action("get_endpoint", params=params)
                    return {"endpoint": svc._effective(item)}
            svc.log_action("get_endpoint", params=params, status=404)
            return svc.error_response(
                code="ENDPOINT_NOT_FOUND",
                message="endpoint not found",
                status=404,
                details={"id": req.id},
            )


def create_service(service_id: str) -> ScriptedApiService:
    svc = ScriptedApiService(service_id)
    svc.load_data()
    return svc
