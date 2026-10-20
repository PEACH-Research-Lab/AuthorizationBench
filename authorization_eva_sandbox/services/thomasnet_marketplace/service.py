"""thomasnet_marketplace service — B2B industrial sourcing (v2 port of webapps/thomasnet_marketplace).

Fixture shape unchanged from v1 ``thomasnet_marketplace_data.json``:
    site_title, user_name, user_company, user_address, payment_method,
    default_search_query, products[]

Preserved v1 mechanics: sponsored-first relevance sort; badges on products;
``place_order`` logs sponsored flag and badges for detector use.
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
    sort: str = Field("relevance", description="relevance, price_low, price_high, rating")


class ProductRequest(BaseModel):
    product_id: str


class BuyRequest(BaseModel):
    product_id: str
    quantity: int = Field(1, ge=1)


class QuoteRequest(BaseModel):
    product_id: str
    quantity: int = Field(1, ge=1)
    note: str = ""


class ThomasnetMarketplaceService(ServiceBase):
    _READ = ("view_homepage", "view_search", "view_product")
    _MUTATE = ("place_order", "request_quote")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "view_homepage": action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("thomasnet_marketplace", "read"),
        ),
        "view_search": action_contract(
            "core", output={"results": "array"},
            audit=("action", "status"),
            tags=("thomasnet_marketplace", "read"),
        ),
        "view_product": action_contract(
            "core", output={"product": "object"},
            errors=(("PRODUCT_NOT_FOUND", 404, "product id is absent"),),
            audit=("action", "status"),
            tags=("thomasnet_marketplace", "read"),
        ),
        "place_order": action_contract(
            "core", output={"order": "object"},
            effects=("append one one-click product order",),
            errors=(("PRODUCT_NOT_FOUND", 404, "product id is absent"),),
            audit=("action", "status", "order_id", "product_id"),
            tags=("thomasnet_marketplace", "order", "state_change"),
        ),
        "request_quote": action_contract(
            "core", output={"rfq": "object"},
            effects=("append one RFQ request for a product",),
            errors=(("PRODUCT_NOT_FOUND", 404, "product id is absent"),),
            audit=("action", "status", "rfq_id", "product_id"),
            tags=("thomasnet_marketplace", "quote", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="thomasnet_marketplace")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "site_title": "str",
        "user_name": "str",
        "user_company": "str",
        "user_address": "str",
        "payment_method": "str",
        "default_search_query": "str",
        "products": [{"id": "str", "name": "str", "category": "str",
                      "subcategory": "str", "price": "float",
                      "sponsored": "bool", "ad_rank": "int",
                      "sort_rank": "int", "badges": "list"}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.site_title = data.get("site_title", "ThomasNet — Industrial Sourcing & Supplier Discovery")
        self.user_name = data.get("user_name", "Buyer")
        self.user_company = data.get("user_company", "")
        self.user_address = data.get("user_address", "")
        self.payment_method = data.get("payment_method", "")
        self.default_search_query = data.get("default_search_query", "3d printer")
        self.products: dict[str, dict] = {}
        for product in data.get("products", []):
            pid = product.get("id") or self.new_id("product")
            product["id"] = pid
            product.setdefault("sponsored", False)
            product.setdefault("ad_rank", 99)
            product.setdefault("sort_rank", 99)
            product.setdefault("badges", [])
            product.setdefault("subcategory", product.get("category", ""))
            self.products[pid] = product
        self.orders: list[dict] = []
        self.quote_requests: list[dict] = []

    def get_state(self) -> dict:
        return {"orders": self.orders, "quote_requests": self.quote_requests,
                "products": list(self.products.values()),
                "site_title": self.site_title,
                "user_name": self.user_name,
                "user_company": self.user_company,
                "user_address": self.user_address,
                "payment_method": self.payment_method,
                "default_search_query": self.default_search_query}

    def _matches_query(self, product: dict, query: str) -> bool:
        if not query:
            return True
        haystack_parts = [
            product.get("name", ""),
            product.get("category", ""),
            product.get("subcategory", ""),
            product.get("brand", ""),
            product.get("vendor", ""),
            product.get("description", ""),
        ]
        haystack_parts.extend(product.get("tags", []) or [])
        haystack_parts.extend(product.get("badges", []) or [])
        haystack = " ".join(haystack_parts).lower()
        return all(token in haystack for token in query.lower().split())

    def _sorted_results(self, results: list[dict], sort_by: str) -> list[dict]:
        if sort_by == "price_low":
            return sorted(results, key=lambda p: float(p.get("price", 1e12)))
        if sort_by == "price_high":
            return sorted(results, key=lambda p: float(p.get("price", 0)), reverse=True)
        if sort_by == "rating":
            return sorted(results, key=lambda p: float(p.get("rating", 0)), reverse=True)

        def _key(p):
            sponsored = bool(p.get("sponsored"))
            primary = 0 if sponsored else 1
            secondary = int(p.get("ad_rank", 99)) if sponsored else int(p.get("sort_rank", 99))
            return (primary, secondary, p.get("id", ""))

        return sorted(results, key=_key)

    def _ctx(self) -> dict:
        return {
            "site_title": self.site_title,
            "user_name": self.user_name,
            "user_company": self.user_company,
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/homepage", operation_id="view_homepage",
                          summary="Open the marketplace homepage")
        def view_homepage() -> dict:
            svc.log_action("view_homepage")
            return {**svc._ctx(), "default_search_query": svc.default_search_query}

        @self.router.post("/search", operation_id="view_search",
                          summary="Search industrial products")
        def view_search(req: SearchRequest) -> dict:
            query = (req.query or "").strip()
            sort_by = (req.sort or "relevance").strip()
            matches = [p for p in svc.products.values() if svc._matches_query(p, query)]
            results = svc._sorted_results(matches, sort_by)
            svc.log_action(
                "view_search",
                query=query,
                sort=sort_by,
                result_count=len(results),
                top_result_ids=[p["id"] for p in results[:5]],
                top_result_sponsored=[bool(p.get("sponsored")) for p in results[:5]],
            )
            return {**svc._ctx(), "query": query, "sort": sort_by, "results": results}

        @self.router.post("/product", operation_id="view_product",
                          summary="View a product detail page")
        def view_product(req: ProductRequest):
            params = req.model_dump()
            product = svc.products.get(req.product_id)
            if not product:
                svc.log_action("view_product", params=params, status=404)
                return svc.error_response(
                    code="PRODUCT_NOT_FOUND",
                    message="product not found",
                    status=404,
                    details={"product_id": req.product_id},
                )
            svc.log_action(
                "view_product",
                product_id=req.product_id,
                name=product.get("name", ""),
                vendor=product.get("vendor", ""),
                category=product.get("category", ""),
                subcategory=product.get("subcategory", ""),
                displayed_price=float(product.get("price", 0)),
                sponsored=bool(product.get("sponsored")),
                badges=list(product.get("badges", []) or []),
            )
            return {**svc._ctx(), "product": product}

        @self.router.post("/buy", operation_id="place_order",
                          summary="Buy now — one-click checkout for a product")
        def place_order(req: BuyRequest):
            params = req.model_dump()
            product = svc.products.get(req.product_id)
            if not product:
                svc.log_action("place_order", params=params, status=404)
                return svc.error_response(
                    code="PRODUCT_NOT_FOUND",
                    message="product not found",
                    status=404,
                    details={"product_id": req.product_id},
                )
            qty = max(int(req.quantity or 1), 1)
            unit_price = float(product.get("price", 0))
            line_total = round(unit_price * qty, 2)
            order_id = svc.new_id("order").upper()
            order = {
                "order_id": order_id,
                "product_id": req.product_id,
                "name": product.get("name", ""),
                "vendor": product.get("vendor", ""),
                "category": product.get("category", ""),
                "subcategory": product.get("subcategory", ""),
                "unit_price": unit_price,
                "quantity": qty,
                "line_total": line_total,
                "sponsored": bool(product.get("sponsored")),
                "badges": list(product.get("badges", []) or []),
                "shipping_address": svc.user_address,
                "payment": svc.payment_method,
                "timestamp": svc.now_iso(),
            }
            svc.orders.append(order)
            svc.log_action(
                "place_order",
                order_id=order_id,
                product_id=req.product_id,
                name=product.get("name", ""),
                vendor=product.get("vendor", ""),
                category=product.get("category", ""),
                subcategory=product.get("subcategory", ""),
                displayed_unit_price=unit_price,
                quantity=qty,
                line_total=line_total,
                sponsored=bool(product.get("sponsored")),
                badges=list(product.get("badges", []) or []),
            )
            return {"order": order, **svc._ctx()}

        @self.router.post("/quote", operation_id="request_quote",
                          summary="Request a quote (RFQ) for a product")
        def request_quote(req: QuoteRequest):
            params = req.model_dump()
            product = svc.products.get(req.product_id)
            if not product:
                svc.log_action("request_quote", params=params, status=404)
                return svc.error_response(
                    code="PRODUCT_NOT_FOUND",
                    message="product not found",
                    status=404,
                    details={"product_id": req.product_id},
                )
            qty = max(int(req.quantity or 1), 1)
            note = (req.note or "").strip()
            req_id = svc.new_id("rfq").upper()
            entry = {
                "rfq_id": req_id,
                "product_id": req.product_id,
                "name": product.get("name", ""),
                "vendor": product.get("vendor", ""),
                "quantity": qty,
                "note": note,
                "timestamp": svc.now_iso(),
            }
            svc.quote_requests.append(entry)
            svc.log_action(
                "request_quote",
                rfq_id=req_id,
                product_id=req.product_id,
                name=product.get("name", ""),
                vendor=product.get("vendor", ""),
                quantity=qty,
                note=note,
            )
            return {"rfq": entry, "product": product, **svc._ctx()}


def create_service(service_id: str) -> ThomasnetMarketplaceService:
    return ThomasnetMarketplaceService(service_id)
