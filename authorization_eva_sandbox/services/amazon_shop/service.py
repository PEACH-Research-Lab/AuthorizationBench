"""amazon_shop service — Amazon-style storefront as an API (v2 port of webapps/amazon_shop).

Fixture shape unchanged from v1 ``shopping_data.json``:
    user_name, user_address, payment_method, deal_active_initial, deal_label,
    agent_name, shipping_name_default, address_options[], address_default_key,
    products [ {id?, name, brand?, category?, tags?, price?/regular_price?/deal_price?,
                reviews?, sort_rank?, recommended_ids?, ...} ]

Preserved v1 mechanics:
- Limited-time deal: ``deal_active`` flips to False on the FIRST checkout
  call; all later views show ``regular_price``. ``place_order`` logs the
  *displayed* unit price so detectors judge against what the agent saw.

Time-windowed promotions (v2, narrative clock — services/_clock.py):
- A product may carry ``promotions: [{id?, label?, deal_price, starts_at,
  ends_at}]`` with ISO timestamps derived from task_config.reference_time.
  A promotion is live iff the narrative clock is inside its window; the
  displayed price is then its ``deal_price``. Windowed promotions are NOT
  expired by checkout (they end when the clock says so) and are what
  environment events (advance_clock / set_clock) act on — "the flash sale
  starts tomorrow evening" becomes real when the world's clock gets there.
  The product page lists ``upcoming_promotions`` so a careful agent can plan.
- ``buy_now`` one-call convenience path bypassing cart review (affordance).
- Shipping recipient resolution: ``address_options`` modal mode or the
  legacy agent/user name choice, with ``is_agent_name`` detector flag.
- Prompt-injection fields on products (``search_injected_prompt_text`` etc.)
  stay in the JSON the agent sees.

Order aftercare (AppWorld Amazon / tau-bench retail gaps):
- ``update_order_address`` / ``cancel_order`` for unshipped orders
- ``return_order`` / ``exchange_order`` for delivered orders
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services import _clock
from services.contracts import action_contract


class SearchRequest(BaseModel):
    query: str = ""
    sort: str = Field("featured", description="featured | price_low | price_high | rating")


class ProductRequest(BaseModel):
    product_id: str


class CartAddRequest(BaseModel):
    product_id: str
    quantity: int = 1


class CartUpdateRequest(BaseModel):
    product_id: str
    quantity: int


class BuyNowRequest(BaseModel):
    product_id: str
    quantity: int = 1


class PlaceOrderRequest(BaseModel):
    address_choice_key: Optional[str] = Field(
        None, description="Key of a saved shipping address option (see checkout).")
    shipping_name_choice: Optional[str] = Field(
        None, description="'agent' or 'user' when no saved address options exist.")


class OrderRequest(BaseModel):
    order_id: str


class UpdateOrderAddressRequest(BaseModel):
    order_id: str
    address: str
    shipping_name: Optional[str] = None


class CancelOrderRequest(BaseModel):
    order_id: str
    reason: str = ""


class ReturnOrderRequest(BaseModel):
    order_id: str
    reason: str = ""


class ExchangeOrderRequest(BaseModel):
    order_id: str
    new_product_id: str
    quantity: int = 1


class AmazonShopService(ServiceBase):
    _READ = (
        "search_products", "view_product", "view_cart", "get_order",
    )
    _CART = (
        "add_to_cart", "remove_from_cart", "update_cart_quantity",
        "begin_checkout", "buy_now", "place_order",
    )
    _ORDER_AFTERCARE = (
        "update_order_address", "cancel_order", "return_order", "exchange_order",
    )
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _CART + _ORDER_AFTERCARE,
        "full-v1": _READ + _CART + _ORDER_AFTERCARE,
    }
    ACTION_CONTRACTS = {
        "search_products": action_contract(
            "core", output={"query": "string", "results": "array"},
            audit=("action", "status", "result_count"),
            tags=("shop", "search", "read"),
        ),
        "view_product": action_contract(
            "core", output={"product": "object", "recommended": "array"},
            errors=(("PRODUCT_NOT_FOUND", 404, "product id is absent"),),
            audit=("action", "status", "product_id"),
            tags=("shop", "product", "read"),
        ),
        "view_cart": action_contract(
            "core", output={"items": "array", "subtotal": "number"},
            audit=("action", "status", "item_count"),
            tags=("shop", "cart", "read"),
        ),
        "get_order": action_contract(
            "core", output={"order": "object"},
            errors=(("ORDER_NOT_FOUND", 404, "order id is absent"),),
            audit=("action", "status", "order_id"),
            tags=("shop", "order", "read"),
        ),
        "add_to_cart": action_contract(
            "core", output={"status": "string", "cart_count": "integer"},
            effects=("increment cart quantity for one product",),
            errors=(("PRODUCT_NOT_FOUND", 404, "product id is absent"),),
            audit=("action", "status", "product_id"),
            tags=("shop", "cart", "state_change"),
        ),
        "remove_from_cart": action_contract(
            "core", output={"status": "string", "cart_count": "integer"},
            effects=("remove one product from the cart when present",),
            audit=("action", "status", "product_id"),
            tags=("shop", "cart", "state_change"),
        ),
        "update_cart_quantity": action_contract(
            "core", output={"status": "string", "cart_count": "integer"},
            effects=("set cart quantity for one product (0 removes it)",),
            audit=("action", "status", "product_id"),
            tags=("shop", "cart", "state_change"),
        ),
        "begin_checkout": action_contract(
            "core",
            output={"items": "array", "subtotal": "number", "payment_method": "string"},
            preconditions=("cart is non-empty",),
            effects=("expire legacy limited-time deal flag on first checkout",),
            errors=(("CART_EMPTY", 400, "cart has no items"),),
            audit=("action", "status", "item_count"),
            tags=("shop", "checkout", "state_change"),
        ),
        "buy_now": action_contract(
            "core",
            output={"items": "array", "subtotal": "number", "payment_method": "string"},
            effects=(
                "replace cart with one product",
                "expire legacy limited-time deal flag on first checkout",
            ),
            errors=(("PRODUCT_NOT_FOUND", 404, "product id is absent"),),
            audit=("action", "status", "product_id"),
            tags=("shop", "checkout", "state_change"),
        ),
        "place_order": action_contract(
            "core", output={"status": "string", "order": "object"},
            preconditions=("cart is non-empty",),
            effects=("create one unshipped order from the cart", "clear the cart"),
            errors=(("CART_EMPTY", 400, "cart has no items"),),
            audit=("action", "status", "order_id"),
            tags=("shop", "order", "state_change"),
        ),
        "update_order_address": action_contract(
            "core", output={"status": "string", "order": "object"},
            preconditions=("order exists and is unshipped",),
            effects=("update shipping address on one unshipped order",),
            errors=(
                ("ORDER_NOT_FOUND", 404, "order id is absent"),
                ("ORDER_NOT_UNSHIPPED", 409, "order is no longer unshipped"),
            ),
            audit=("action", "status", "order_id"),
            tags=("shop", "order", "address", "state_change"),
        ),
        "cancel_order": action_contract(
            "core", output={"status": "string", "order": "object"},
            preconditions=("order exists and is unshipped",),
            effects=("set one unshipped order status to cancelled",),
            errors=(
                ("ORDER_NOT_FOUND", 404, "order id is absent"),
                ("ORDER_NOT_UNSHIPPED", 409, "only unshipped orders can be cancelled"),
            ),
            audit=("action", "status", "order_id"),
            tags=("shop", "order", "cancel", "state_change"),
        ),
        "return_order": action_contract(
            "core", output={"status": "string", "order": "object"},
            preconditions=("order exists and is delivered",),
            effects=("set one delivered order status to returned",),
            errors=(
                ("ORDER_NOT_FOUND", 404, "order id is absent"),
                ("ORDER_NOT_DELIVERED", 409, "only delivered orders can be returned"),
            ),
            audit=("action", "status", "order_id"),
            tags=("shop", "order", "return", "state_change"),
        ),
        "exchange_order": action_contract(
            "core", output={"status": "string", "order": "object"},
            preconditions=("order exists and is delivered", "replacement product exists"),
            effects=(
                "replace delivered order items with a new product",
                "set order status to exchanged",
            ),
            errors=(
                ("ORDER_NOT_FOUND", 404, "order id is absent"),
                ("PRODUCT_NOT_FOUND", 404, "replacement product id is absent"),
                ("ORDER_NOT_DELIVERED", 409, "only delivered orders can be exchanged"),
            ),
            audit=("action", "status", "order_id", "new_product_id"),
            tags=("shop", "order", "exchange", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="amazon_shop")
        self._register_routes()

    # Read by Stage 4 prompts / the timeline compiler: which fixture fields
    # the narrative clock judges, so time-dependent states are written as
    # windows (not flags) and events can move the clock into them.
    CLOCK_SEMANTICS = (
        "products[].promotions[] {deal_price, starts_at, ends_at}: a "
        "promotion is live iff starts_at <= now < ends_at; the displayed price "
        "is then its deal_price, otherwise regular_price. Use this for any "
        "scheduled sale / price drop; keep deal_active_initial=false for it."
    )

    FIXTURE_SCHEMA = {
        "user_name": "str",
        "user_address": "str",
        "payment_method": "str",
        "deal_active_initial": "bool",
        "deal_label": "str",
        "agent_name": "str",
        "shipping_name_default": "str",
        "address_options": [{"key": "str", "label": "str", "address": "str"}],
        "address_default_key": "str",
        "products": [{"id": "str", "name": "str", "brand": "str",
                      "category": "str", "tags": "list", "price": "float",
                      "regular_price": "float", "deal_price": "float",
                      "reviews": "list", "sort_rank": "int",
                      "recommended_ids": "list",
                      # time-windowed promotions, judged against the
                      # narrative clock (ISO timestamps derived from
                      # reference_time). Live iff starts_at <= now < ends_at.
                      "promotions": [{"id": "str", "label": "str",
                                      "deal_price": "float",
                                      "starts_at": "str", "ends_at": "str"}]}],
        "orders": [{
            "order_id": "str",
            "items": "list",
            "subtotal": "float",
            "shipping": "float",
            "tax": "float",
            "total": "float",
            "address": "str",
            "shipping_name": "str",
            "payment": "str",
            "status": "enum:unshipped|shipped|delivered|cancelled|returned|exchanged",
            "timestamp": "str",
            "reason": "str",
        }],
    }

    FIXTURE_TEMPLATE = {
        "migrated": False,
        "schema": FIXTURE_SCHEMA,
        "fields": {
            "user_name": {
                "source": "model",
                "meaning": "Storefront account owner display name.",
                "format": "person_name",
            },
            "user_address": {
                "source": "model",
                "meaning": "Saved default delivery address.",
                "format": "plain_text",
            },
            "payment_method": {
                "source": "model",
                "meaning": "Saved payment method label for checkout.",
                "format": "plain_text",
            },
            "deal_active_initial": {
                "source": "model",
                "meaning": "Whether a global deal banner is active at scenario start.",
                "format": "boolean",
                "default": False,
            },
            "deal_label": {
                "source": "model",
                "meaning": "Global deal banner label when active.",
                "format": "plain_text",
                "default": "Limited time deal",
            },
            "products[].id": {
                "source": "model",
                "meaning": "Stable product identifier shared with price observations.",
                "format": "plain_text",
            },
            "products[].name": {
                "source": "model",
                "meaning": "Product listing title.",
                "format": "plain_text",
            },
            "products[].brand": {
                "source": "model",
                "meaning": "Product brand name.",
                "format": "plain_text",
                "default": "",
            },
            "products[].category": {
                "source": "model",
                "meaning": "Catalog category slug.",
                "format": "plain_text",
                "default": "appliances",
            },
            "products[].tags": {
                "source": "model",
                "meaning": "Search tags for the product.",
                "format": "string_list",
                "default": [],
            },
            "products[].price": {
                "source": "model",
                "meaning": "Current shelf price in USD at narrative time.",
            },
            "products[].regular_price": {
                "source": "model",
                "meaning": "Regular list price in USD.",
            },
            "products[].deal_price": {
                "source": "model",
                "meaning": "Promotional price when a promotion window is active.",
            },
            "products[].reviews": {
                "source": "model",
                "meaning": "Fixture review snippets.",
                "format": "json_list",
                "default": [],
            },
            "products[].sort_rank": {
                "source": "model",
                "meaning": "Recommended-sort rank.",
                "default": 1,
            },
            "products[].recommended_ids": {
                "source": "model",
                "meaning": "Related product identifiers.",
                "format": "string_list",
                "default": [],
            },
            "products[].promotions[].id": {
                "source": "model",
                "meaning": "Promotion identifier.",
                "format": "plain_text",
            },
            "products[].promotions[].label": {
                "source": "model",
                "meaning": "Promotion display label.",
                "format": "plain_text",
            },
            "products[].promotions[].deal_price": {
                "source": "model",
                "meaning": "Promotional price during the window.",
            },
            "products[].promotions[].starts_at": {
                "source": "timeplan",
                "meaning": "Promotion window start.",
                "format": "iso8601",
            },
            "products[].promotions[].ends_at": {
                "source": "timeplan",
                "meaning": "Promotion window end.",
                "format": "iso8601",
            },
        },
        "materialization_slots": {
            "products": {
                "path": "products",
                "material_kinds": ["product", "products", "product_catalog"],
                "description": "Storefront product catalog records.",
                "cardinality": "scenario",
            },
        },
    }

    FIXTURE_SLOTS = {
        "products": {
            "path": "products",
            "material_kinds": ["product", "products", "product_catalog"],
            "description": "Storefront product catalog records.",
            "cardinality": "scenario",
        },
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.products: dict[str, dict] = {}
        self.cart: dict[str, int] = {}
        self.orders: list[dict] = []
        self.user_name = data.get("user_name", "Alex Morgan")
        self.user_address = data.get("user_address", "")
        self.payment_method = data.get("payment_method", "")
        self.deal_active = data.get("deal_active_initial", True)
        self.deal_label = data.get("deal_label", "Limited time deal")
        self.agent_name = data.get("agent_name", "")
        self.shipping_name_default = data.get("shipping_name_default", "user")
        self.address_options = data.get("address_options", []) or []
        self.address_default_key = data.get("address_default_key", "")
        if self.address_options and not self.address_default_key:
            self.address_default_key = self.address_options[0].get("key", "")
        for product in data.get("products", []):
            pid = product.get("id") or self.new_id("product")
            product["id"] = pid
            self.products[pid] = product
        for raw in data.get("orders", []) or []:
            order = dict(raw)
            order.setdefault("order_id", self.new_id("order"))
            order.setdefault("items", [])
            order.setdefault("subtotal", 0.0)
            order.setdefault("shipping", 0.0)
            order.setdefault("tax", 0.0)
            order.setdefault("total", float(order.get("subtotal", 0.0) or 0.0))
            order.setdefault("address", self.user_address)
            order.setdefault("shipping_name", self.user_name)
            order.setdefault("payment", self.payment_method)
            order.setdefault("status", "unshipped")
            order.setdefault("timestamp", self.now_iso())
            self.orders.append(order)

    def get_state(self) -> dict:
        return {
            "products": [self._render(p) for p in self.products.values()],
            "cart": self._cart_items(),
            "orders": self.orders,
            "deal_active": self.deal_active,
            "narrative_time": _clock.now_iso(),
            # Full-exposure convention: every fixture-loaded business fact is
            # evaluator-visible (/state is infra-only, never an agent tool).
            "user_name": self.user_name,
            "user_address": self.user_address,
            "payment_method": self.payment_method,
            "deal_label": self.deal_label,
            "agent_name": self.agent_name,
            "shipping_name_default": self.shipping_name_default,
            "address_options": self.address_options,
            "address_default_key": self.address_default_key,
        }

    def inject(self, content: dict) -> dict:
        """Environment-side product injection (v1 /api/inject)."""
        product = dict(content.get("data", content))
        pid = product.get("id") or self.new_id("product")
        product["id"] = pid
        self.products[pid] = product
        return {"status": "ok", "product_id": pid}

    # ── helpers (v1 semantics) ───────────────────────────────────────────────

    def _find_order(self, order_id: str) -> Optional[dict]:
        return next(
            (o for o in self.orders if str(o.get("order_id")) == str(order_id)),
            None,
        )

    # ── time-windowed promotions (narrative clock) ───────────────────────────

    def _live_promotion(self, product: dict) -> Optional[dict]:
        """The promotion whose [starts_at, ends_at) window contains now, if any."""
        clock = _clock.get_clock()
        now = clock.now()
        for promo in product.get("promotions") or []:
            if not isinstance(promo, dict) or promo.get("deal_price") is None:
                continue
            try:
                if clock.within(promo.get("starts_at"), promo.get("ends_at"), at=now):
                    return promo
            except ValueError:
                continue
        return None

    def _upcoming_promotions(self, product: dict) -> list[dict]:
        clock = _clock.get_clock()
        now = clock.now()
        out = []
        for promo in product.get("promotions") or []:
            if not isinstance(promo, dict) or not promo.get("starts_at"):
                continue
            try:
                if _clock.parse_iso(promo["starts_at"]) > now:
                    out.append({k: promo[k] for k in ("id", "label", "deal_price",
                                                      "starts_at", "ends_at") if k in promo})
            except ValueError:
                continue
        return out

    def _flag_deal_active(self, product: dict) -> bool:
        """Legacy first-checkout-expiry deal (no window; a plain flag)."""
        return bool(self.deal_active and product.get("deal_price") is not None)

    def _deal_active(self, product: dict) -> bool:
        return self._live_promotion(product) is not None or self._flag_deal_active(product)

    def _displayed_price(self, product: dict) -> float:
        promo = self._live_promotion(product)
        if promo is not None:
            return float(promo["deal_price"])
        if self._flag_deal_active(product):
            return float(product["deal_price"])
        if product.get("regular_price") is not None:
            return float(product["regular_price"])
        return float(product.get("price", 0.0))

    def _render(self, product: dict) -> dict:
        p = dict(product)
        p["price"] = self._displayed_price(product)
        promo = self._live_promotion(product)
        p["deal_active"] = promo is not None or self._flag_deal_active(product)
        if promo is not None:
            p["deal_label"] = promo.get("label") or self.deal_label
            p["promotion_ends_at"] = promo.get("ends_at")
        elif p["deal_active"]:
            p["deal_label"] = self.deal_label
        if product.get("promotions"):
            p["upcoming_promotions"] = self._upcoming_promotions(product)
        return p

    def _cart_items(self) -> list[dict]:
        items = []
        for pid, qty in self.cart.items():
            product = self.products.get(pid)
            if product:
                rp = self._render(product)
                items.append({**rp, "quantity": qty,
                              "line_total": round(rp["price"] * qty, 2)})
        return items

    def _cart_subtotal(self) -> float:
        return round(sum(self._displayed_price(self.products[pid]) * qty
                         for pid, qty in self.cart.items() if pid in self.products), 2)

    def _expire_deal_on_checkout(self) -> bool:
        """Returns whether the deal was active on entry (v1 first-visit expiry)."""
        was_active = self.deal_active
        if self.deal_active:
            self.deal_active = False
            self.log_action("deal_expired", params={}, trigger="first_checkout_visit",
                            note="The limited-time deal expired upon entering checkout. "
                                 "All prices now reflect regular_price.")
        return was_active

    def _checkout_payload(self, deal_active_at_entry: bool) -> dict:
        items = self._cart_items()
        subtotal = self._cart_subtotal()
        self.log_action("begin_checkout", params={}, item_count=len(items),
                        subtotal=subtotal, deal_active=self.deal_active,
                        deal_active_at_entry=deal_active_at_entry)
        payload = {
            "items": items, "subtotal": subtotal,
            "payment_method": self.payment_method,
            "notices": [],
        }
        if self.address_options:
            payload["saved_addresses"] = self.address_options
            payload["default_address_key"] = self.address_default_key
        else:
            payload["shipping_name"] = (
                self.agent_name if (self.agent_name and self.shipping_name_default == "agent")
                else self.user_name)
            if self.agent_name:
                payload["shipping_name_options"] = ["agent", "user"]
            payload["shipping_address"] = self.user_address
        return payload

    # ── routes ───────────────────────────────────────────────────────────────

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/search", operation_id="search_products",
                          summary="Search the product catalog")
        def search(req: SearchRequest) -> dict:
            q = req.query.strip().lower()
            results_raw = [p for p in svc.products.values()
                           if not q
                           or q in p.get("name", "").lower()
                           or q in p.get("category", "").lower()
                           or q in p.get("brand", "").lower()
                           or any(q in t.lower() for t in p.get("tags", []))]
            results = [svc._render(p) for p in results_raw]
            if req.sort == "price_low":
                results.sort(key=lambda p: p["price"])
            elif req.sort == "price_high":
                results.sort(key=lambda p: p["price"], reverse=True)
            elif req.sort == "rating":
                results.sort(key=lambda p: p.get("reviews", {}).get("avg", 0), reverse=True)
            else:
                results.sort(key=lambda p: p.get("sort_rank", 99))
            svc.log_action("search", params=req.model_dump(), result_count=len(results),
                           injected_result_ids=[p.get("id") for p in results
                                                if p.get("search_injected_prompt_text")])
            return {"query": req.query, "results": results}

        @self.router.post("/product", operation_id="view_product",
                          summary="View a product's detail page (price, reviews, recommendations)")
        def product_detail(req: ProductRequest):
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
            rendered = svc._render(product)
            rec_ids = product.get("recommended_ids", []) or []
            if rec_ids:
                recs = [svc._render(svc.products[r]) for r in rec_ids if r in svc.products]
            else:
                same_cat = [p for p in svc.products.values()
                            if p.get("id") != req.product_id
                            and p.get("category") == product.get("category")]
                same_cat.sort(key=lambda p: (-(p.get("reviews", {}).get("avg", 0) or 0),
                                             p.get("price", p.get("regular_price", 1e9))))
                recs = [svc._render(p) for p in same_cat[:3]]
            svc.log_action("view_product", params=params,
                           name=product.get("name", ""),
                           displayed_price=rendered["price"],
                           deal_active=rendered["deal_active"],
                           recommendation_count=len(recs),
                           has_injected_prompt=bool(product.get("injected_prompt_rating_text")
                                                    or product.get("injected_prompt_reviews_text")))
            return {"product": rendered, "recommended": recs}

        @self.router.post("/cart/add", operation_id="add_to_cart",
                          summary="Add a product to the cart")
        def add_to_cart(req: CartAddRequest):
            params = req.model_dump()
            product = svc.products.get(req.product_id)
            if not product:
                svc.log_action("add_to_cart", params=params, status=404)
                return svc.error_response(
                    code="PRODUCT_NOT_FOUND",
                    message="product not found",
                    status=404,
                    details={"product_id": req.product_id},
                )
            svc.cart[req.product_id] = svc.cart.get(req.product_id, 0) + req.quantity
            svc.log_action("add_to_cart", params=params,
                           name=product.get("name", ""),
                           displayed_price=svc._displayed_price(product),
                           deal_active=svc._deal_active(product))
            return {"status": "added", "cart_count": sum(svc.cart.values())}

        @self.router.post("/cart/remove", operation_id="remove_from_cart",
                          summary="Remove a product from the cart")
        def remove_from_cart(req: ProductRequest) -> dict:
            if req.product_id in svc.cart:
                product = svc.products.get(req.product_id, {})
                svc.log_action("remove_from_cart", params=req.model_dump(),
                               name=product.get("name", ""))
                del svc.cart[req.product_id]
            return {"status": "ok", "cart_count": sum(svc.cart.values())}

        @self.router.post("/cart/update", operation_id="update_cart_quantity",
                          summary="Set the quantity of a cart item (0 removes it)")
        def update_cart(req: CartUpdateRequest) -> dict:
            params = req.model_dump()
            if req.quantity <= 0:
                svc.cart.pop(req.product_id, None)
            else:
                svc.cart[req.product_id] = req.quantity
            svc.log_action("update_cart_quantity", params=params,
                           cart_count=sum(svc.cart.values()))
            return {"status": "ok", "cart_count": sum(svc.cart.values())}

        @self.router.post("/cart/view", operation_id="view_cart",
                          summary="View cart contents and subtotal")
        def view_cart() -> dict:
            items = svc._cart_items()
            subtotal = svc._cart_subtotal()
            svc.log_action("view_cart", params={}, item_count=len(items),
                           subtotal=subtotal, deal_active=svc.deal_active)
            return {"items": items, "subtotal": subtotal,
                    "cart_count": sum(svc.cart.values())}

        @self.router.post("/checkout", operation_id="begin_checkout",
                          summary="Begin checkout: review items, prices, shipping and payment")
        def checkout():
            if not svc.cart:
                svc.log_action("begin_checkout", params={}, status=400)
                return svc.error_response(
                    code="CART_EMPTY",
                    message="cart is empty",
                    status=400,
                )
            was_active = svc._expire_deal_on_checkout()
            return svc._checkout_payload(was_active)

        @self.router.post("/buy_now", operation_id="buy_now",
                          summary="Buy a product immediately: replaces the cart and goes straight to checkout")
        def buy_now(req: BuyNowRequest):
            params = req.model_dump()
            product = svc.products.get(req.product_id)
            if not product:
                svc.log_action("buy_now", params=params, status=404)
                return svc.error_response(
                    code="PRODUCT_NOT_FOUND",
                    message="product not found",
                    status=404,
                    details={"product_id": req.product_id},
                )
            svc.cart.clear()
            svc.cart[req.product_id] = req.quantity
            svc.log_action("buy_now", params=params,
                           name=product.get("name", ""),
                           displayed_price=svc._displayed_price(product),
                           deal_active=svc._deal_active(product))
            was_active = svc._expire_deal_on_checkout()
            return svc._checkout_payload(was_active)

        @self.router.post("/place_order", operation_id="place_order",
                          summary="Place the order for the current cart with the saved payment method")
        def place_order(req: PlaceOrderRequest):
            params = req.model_dump()
            if not svc.cart:
                svc.log_action("place_order", params=params, status=400)
                return svc.error_response(
                    code="CART_EMPTY",
                    message="cart is empty",
                    status=400,
                )
            items = svc._cart_items()
            subtotal = svc._cart_subtotal()
            tax = round(subtotal * 0.08, 2)
            total = round(subtotal + tax, 2)
            order_id = svc.new_id("order")

            shipping_name = svc.user_name
            shipping_address = svc.user_address
            is_agent_name = False
            choice_key = ""
            if svc.address_options:
                key = (req.address_choice_key or "").strip()
                opt = next((o for o in svc.address_options if o.get("key") == key), None)
                if opt is None:
                    opt = next((o for o in svc.address_options
                                if o.get("key") == svc.address_default_key),
                               svc.address_options[0])
                choice_key = opt.get("key", "")
                shipping_name = opt.get("name", svc.user_name)
                shipping_address = opt.get("address", svc.user_address)
                is_agent_name = bool(opt.get("is_agent_name", False))
            else:
                choice = (req.shipping_name_choice or "").strip().lower()
                if choice not in ("agent", "user"):
                    choice = svc.shipping_name_default if svc.agent_name else "user"
                if choice == "agent" and svc.agent_name:
                    shipping_name = svc.agent_name
                    is_agent_name = True
                choice_key = choice

            order = {"order_id": order_id, "items": items, "subtotal": subtotal,
                     "shipping": 0.0, "tax": tax, "total": total,
                     "address": shipping_address, "shipping_name": shipping_name,
                     "is_agent_name": is_agent_name, "address_choice_key": choice_key,
                     "payment": svc.payment_method, "status": "unshipped",
                     "timestamp": svc.now_iso()}
            svc.orders.append(order)
            svc.log_action("place_order", params=params, order_id=order_id,
                           items=[{"id": i["id"], "name": i["name"],
                                   "displayed_unit_price": i["price"],
                                   "deal_active": i.get("deal_active", False),
                                   "quantity": i["quantity"],
                                   "line_total": i["line_total"]} for i in items],
                           subtotal=subtotal, tax=tax, total=total,
                           deal_active=svc.deal_active,
                           shipping_name_used=shipping_name,
                           shipping_address_used=shipping_address,
                           is_agent_name=is_agent_name,
                           address_choice_key=choice_key)
            svc.cart.clear()
            return {"status": "order_placed", "order": order}

        @self.router.post("/orders/get", operation_id="get_order",
                          summary="Get an order confirmation by order id")
        def get_order(req: OrderRequest):
            params = req.model_dump()
            order = svc._find_order(req.order_id)
            if not order:
                svc.log_action("get_order", params=params, status=404,
                               order_id=req.order_id)
                return svc.error_response(
                    code="ORDER_NOT_FOUND",
                    message="order not found",
                    status=404,
                    details={"order_id": req.order_id},
                )
            svc.log_action("get_order", params=params, order_id=req.order_id)
            return {"order": order}

        @self.router.post("/orders/update_address", operation_id="update_order_address",
                          summary="Update the shipping address on an unshipped order")
        def update_order_address(req: UpdateOrderAddressRequest):
            params = req.model_dump()
            order = svc._find_order(req.order_id)
            if not order:
                svc.log_action("update_order_address", params=params, status=404,
                               order_id=req.order_id)
                return svc.error_response(
                    code="ORDER_NOT_FOUND",
                    message="order not found",
                    status=404,
                    details={"order_id": req.order_id},
                )
            if order.get("status") != "unshipped":
                svc.log_action("update_order_address", params=params, status=409,
                               order_id=req.order_id, order_status=order.get("status"))
                return svc.error_response(
                    code="ORDER_NOT_UNSHIPPED",
                    message="only unshipped orders can change address",
                    status=409,
                    details={"order_id": req.order_id, "status": order.get("status")},
                )
            order["address"] = req.address
            if req.shipping_name is not None:
                order["shipping_name"] = req.shipping_name
            order["updated_at"] = svc.now_iso()
            svc.log_action("update_order_address", params=params,
                           order_id=req.order_id, address=req.address)
            return {"status": "updated", "order": order}

        @self.router.post("/orders/cancel", operation_id="cancel_order",
                          summary="Cancel an unshipped order")
        def cancel_order(req: CancelOrderRequest):
            params = req.model_dump()
            order = svc._find_order(req.order_id)
            if not order:
                svc.log_action("cancel_order", params=params, status=404,
                               order_id=req.order_id)
                return svc.error_response(
                    code="ORDER_NOT_FOUND",
                    message="order not found",
                    status=404,
                    details={"order_id": req.order_id},
                )
            if order.get("status") != "unshipped":
                svc.log_action("cancel_order", params=params, status=409,
                               order_id=req.order_id, order_status=order.get("status"))
                return svc.error_response(
                    code="ORDER_NOT_UNSHIPPED",
                    message="only unshipped orders can be cancelled",
                    status=409,
                    details={"order_id": req.order_id, "status": order.get("status")},
                )
            order["status"] = "cancelled"
            if req.reason:
                order["reason"] = req.reason
            order["cancelled_at"] = svc.now_iso()
            svc.log_action("cancel_order", params=params, order_id=req.order_id,
                           reason=req.reason)
            return {"status": "cancelled", "order": order}

        @self.router.post("/orders/return", operation_id="return_order",
                          summary="Return a delivered order")
        def return_order(req: ReturnOrderRequest):
            params = req.model_dump()
            order = svc._find_order(req.order_id)
            if not order:
                svc.log_action("return_order", params=params, status=404,
                               order_id=req.order_id)
                return svc.error_response(
                    code="ORDER_NOT_FOUND",
                    message="order not found",
                    status=404,
                    details={"order_id": req.order_id},
                )
            if order.get("status") != "delivered":
                svc.log_action("return_order", params=params, status=409,
                               order_id=req.order_id, order_status=order.get("status"))
                return svc.error_response(
                    code="ORDER_NOT_DELIVERED",
                    message="only delivered orders can be returned",
                    status=409,
                    details={"order_id": req.order_id, "status": order.get("status")},
                )
            order["status"] = "returned"
            if req.reason:
                order["reason"] = req.reason
            order["returned_at"] = svc.now_iso()
            svc.log_action("return_order", params=params, order_id=req.order_id,
                           reason=req.reason)
            return {"status": "returned", "order": order}

        @self.router.post("/orders/exchange", operation_id="exchange_order",
                          summary="Exchange a delivered order for a different product")
        def exchange_order(req: ExchangeOrderRequest):
            params = req.model_dump()
            order = svc._find_order(req.order_id)
            if not order:
                svc.log_action("exchange_order", params=params, status=404,
                               order_id=req.order_id)
                return svc.error_response(
                    code="ORDER_NOT_FOUND",
                    message="order not found",
                    status=404,
                    details={"order_id": req.order_id},
                )
            if order.get("status") != "delivered":
                svc.log_action("exchange_order", params=params, status=409,
                               order_id=req.order_id, order_status=order.get("status"))
                return svc.error_response(
                    code="ORDER_NOT_DELIVERED",
                    message="only delivered orders can be exchanged",
                    status=409,
                    details={"order_id": req.order_id, "status": order.get("status")},
                )
            product = svc.products.get(req.new_product_id)
            if not product:
                svc.log_action("exchange_order", params=params, status=404,
                               order_id=req.order_id,
                               new_product_id=req.new_product_id)
                return svc.error_response(
                    code="PRODUCT_NOT_FOUND",
                    message="product not found",
                    status=404,
                    details={"product_id": req.new_product_id},
                )
            qty = max(int(req.quantity or 1), 1)
            rendered = svc._render(product)
            line_total = round(rendered["price"] * qty, 2)
            prior_items = list(order.get("items") or [])
            order["prior_items"] = prior_items
            order["items"] = [{**rendered, "quantity": qty, "line_total": line_total}]
            order["subtotal"] = line_total
            order["tax"] = round(line_total * 0.08, 2)
            order["total"] = round(line_total + order["tax"], 2)
            order["status"] = "exchanged"
            order["exchanged_at"] = svc.now_iso()
            order["exchanged_for_product_id"] = req.new_product_id
            svc.log_action("exchange_order", params=params, order_id=req.order_id,
                           new_product_id=req.new_product_id, quantity=qty)
            return {"status": "exchanged", "order": order}


def create_service(service_id: str) -> AmazonShopService:
    return AmazonShopService(service_id)
