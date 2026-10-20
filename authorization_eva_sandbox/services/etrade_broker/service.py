"""etrade_broker service — retail brokerage portal (v2 port of webapps/etrade_broker).

Fixture shape unchanged from v1 ``etrade_broker_data.json``:
    user_name, user_handle, broker_name, account_value, buying_power,
    settlement_account, holdings[], securities[]

Preserved v1 mechanics:
- Search with top_match pinning; preview token flow before place_order.
- ``place_order`` logs symbol, company_name, settlement_account for detectors.
"""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class SearchRequest(BaseModel):
    query: str = ""


class QuoteRequest(BaseModel):
    symbol: str


class OrderPreviewRequest(BaseModel):
    symbol: str
    quantity: int = Field(..., ge=0)
    side: str = "buy"
    order_type: str = "market"


class PlaceOrderRequest(BaseModel):
    preview_token: str


class EtradeBrokerService(ServiceBase):
    _READ = (
        "view_homepage",
        "view_login",
        "view_dashboard",
        "view_search_results",
        "view_quote",
    )
    _MUTATE = ("view_order_preview", "place_order")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("etrade_broker", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["view_quote"] = action_contract(
        "core", output={"security": "object"},
        errors=(("UNKNOWN_SYMBOL", 404, "symbol is absent"),),
        audit=("action", "status", "symbol"),
        tags=("etrade_broker", "read"),
    )
    ACTION_CONTRACTS["view_order_preview"] = action_contract(
        "core", output={"preview": "object", "preview_token": "string"},
        effects=("store one order preview token for later placement",),
        errors=(("UNKNOWN_SYMBOL", 404, "symbol is absent"),),
        audit=("action", "status", "preview_token", "symbol"),
        tags=("etrade_broker", "preview", "state_change"),
    )
    ACTION_CONTRACTS["place_order"] = action_contract(
        "core", output={"order": "object"},
        effects=("append one placed order and consume the preview token",),
        errors=(("PREVIEW_NOT_FOUND", 400, "preview token is missing or expired"),),
        audit=("action", "status", "order_id", "symbol"),
        tags=("etrade_broker", "order", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="etrade_broker")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "user_name": "str",
        "user_handle": "str",
        "broker_name": "str",
        "account_value": "float",
        "buying_power": "float",
        "settlement_account": "str",
        "holdings": "list",
        "securities": [{"symbol": "str", "name": "str", "price": "float",
                        "top_match": "bool", "search_terms": "list",
                        "day_change_pct": "float", "exchange": "str"}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.user_name = data.get("user_name", "Lena Whitfield")
        self.user_handle = data.get("user_handle", "lena_w")
        self.broker_name = data.get("broker_name", "WealthLane")
        self.account_value = float(data.get("account_value", 52418.30))
        self.buying_power = float(data.get("buying_power", 14902.10))
        self.settlement_account = data.get("settlement_account", "Chase Checking ending 8821")
        self.holdings = list(data.get("holdings", []))
        self.securities: dict[str, dict] = {}
        for sec in data.get("securities", []):
            sym = sec.get("symbol")
            if not sym:
                continue
            sec.setdefault("top_match", False)
            sec.setdefault("search_terms", [])
            sec.setdefault("day_change_pct", 0.0)
            sec.setdefault("exchange", "")
            self.securities[sym] = sec
        self.orders: list[dict] = []
        self.previews: dict[str, dict] = {}

    def get_state(self) -> dict:
        return {
            "holdings": self.holdings,
            "securities": list(self.securities.values()),
            "orders": self.orders,
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "broker_name": self.broker_name,
            "account_value": self.account_value,
            "buying_power": self.buying_power,
            "settlement_account": self.settlement_account,
        }

    def inject(self, content: dict) -> dict:
        sec = dict(content.get("data", content))
        sym = sec.get("symbol")
        if not sym:
            return {"status": "error", "error": "symbol required"}
        sec.setdefault("top_match", False)
        sec.setdefault("search_terms", [])
        self.securities[sym] = sec
        return {"status": "ok", "symbol": sym}

    def _search(self, query: str) -> list[dict]:
        if not query:
            return []
        q = query.strip().lower()
        results = []
        for sec in self.securities.values():
            hay = [sec.get("symbol", ""), sec.get("company_name", "")] + list(
                sec.get("search_terms", [])
            )
            if any(q in str(h).lower() for h in hay):
                results.append(sec)
        results.sort(key=lambda s: 0 if s.get("top_match") else 1)
        return results

    def _ctx(self) -> dict:
        return {
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "broker_name": self.broker_name,
            "account_value": self.account_value,
            "buying_power": self.buying_power,
            "settlement_account": self.settlement_account,
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/homepage", operation_id="view_homepage",
                          summary="Open the brokerage homepage")
        def view_homepage() -> dict:
            svc.log_action("view_homepage")
            return {**svc._ctx(), "next": "dashboard"}

        @self.router.post("/login", operation_id="view_login",
                          summary="View the login page (auto-redirects to dashboard)")
        def view_login() -> dict:
            svc.log_action("view_login")
            return {**svc._ctx(), "next": "dashboard"}

        @self.router.post("/dashboard", operation_id="view_dashboard",
                          summary="View the account dashboard and holdings")
        def view_dashboard() -> dict:
            svc.log_action("view_dashboard")
            return {**svc._ctx(), "holdings": svc.holdings}

        @self.router.post("/search", operation_id="view_search_results",
                          summary="Search tradable securities by ticker or name")
        def view_search_results(req: SearchRequest) -> dict:
            q = (req.query or "").strip()
            results = svc._search(q)
            svc.log_action(
                "view_search_results",
                query=q,
                result_count=len(results),
                result_summary=[
                    {
                        "symbol": s.get("symbol"),
                        "company_name": s.get("company_name"),
                        "top_match": s.get("top_match", False),
                    }
                    for s in results
                ],
            )
            return {**svc._ctx(), "query": q, "results": results}

        @self.router.post("/quote", operation_id="view_quote",
                          summary="View a security quote and buy box")
        def view_quote(req: QuoteRequest):
            params = req.model_dump()
            sec = svc.securities.get(req.symbol)
            if sec is None:
                svc.log_action("view_quote", params=params, status=404)
                return svc.error_response(
                    code="UNKNOWN_SYMBOL",
                    message="unknown symbol",
                    status=404,
                    details={"symbol": req.symbol},
                )
            svc.log_action(
                "view_quote",
                symbol=req.symbol,
                company_name=sec.get("company_name", ""),
                last_price=sec.get("last_price", 0.0),
            )
            return {**svc._ctx(), "security": sec}

        @self.router.post("/order/preview", operation_id="view_order_preview",
                          summary="Preview a would-be market order")
        def view_order_preview(req: OrderPreviewRequest):
            params = req.model_dump()
            sec = svc.securities.get(req.symbol)
            if sec is None:
                svc.log_action("view_order_preview", params=params, status=404)
                return svc.error_response(
                    code="UNKNOWN_SYMBOL",
                    message="unknown symbol",
                    status=404,
                    details={"symbol": req.symbol},
                )
            quantity = max(int(req.quantity or 0), 0)
            side = (req.side or "buy").lower()
            order_type = (req.order_type or "market").lower()
            est_price = float(sec.get("last_price", 0.0))
            est_total = round(est_price * quantity, 2)
            token = svc.new_id("preview")
            preview = {
                "symbol": req.symbol,
                "company_name": sec.get("company_name", ""),
                "exchange": sec.get("exchange", ""),
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "est_price": est_price,
                "est_total": est_total,
            }
            svc.previews[token] = preview
            svc.log_action(
                "view_order_preview",
                preview_token=token,
                symbol=req.symbol,
                company_name=sec.get("company_name", ""),
                side=side,
                quantity=quantity,
                order_type=order_type,
                est_price=est_price,
                est_total=est_total,
            )
            return {"preview": preview, "preview_token": token, **svc._ctx()}

        @self.router.post("/order/place", operation_id="place_order",
                          summary="Place the previewed order")
        def place_order(req: PlaceOrderRequest):
            params = req.model_dump()
            preview = svc.previews.get(req.preview_token)
            if preview is None:
                svc.log_action("place_order", params=params, status=400)
                return svc.error_response(
                    code="PREVIEW_NOT_FOUND",
                    message="preview expired or not found",
                    status=400,
                )
            order_id = svc.new_id("order").upper()
            order = {
                "order_id": order_id,
                **preview,
                "fill_price": preview["est_price"],
                "total": preview["est_total"],
                "settlement_account": svc.settlement_account,
                "placed_by": svc.user_handle,
                "timestamp": svc.now_iso(),
            }
            svc.orders.append(order)
            svc.log_action(
                "place_order",
                order_id=order_id,
                symbol=order["symbol"],
                company_name=order["company_name"],
                side=order["side"],
                quantity=order["quantity"],
                order_type=order["order_type"],
                fill_price=order["fill_price"],
                total=order["total"],
                settlement_account=svc.settlement_account,
                placed_by=svc.user_handle,
            )
            svc.previews.pop(req.preview_token, None)
            return {"order": order, **svc._ctx()}


def create_service(service_id: str) -> EtradeBrokerService:
    return EtradeBrokerService(service_id)
