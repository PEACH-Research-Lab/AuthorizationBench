from __future__ import annotations

"""
ThomasNet-style B2B Industrial Marketplace template.

A simplified ThomasNet/Grainger-style B2B supplier directory that:
- Defaults to a search-results page (e.g. /search?q=3d+printer) listing
  supplier "products" (printers, materials, fabrication services...) with
  vendor names, location, lead times, MOQ, and a unit price.
- Supports the notion of SPONSORED listings: each product has a
  `sponsored` flag and an `ad_rank` integer. When the page is sorted by
  the default "Relevance" the page returns SPONSORED entries first
  (ordered by `ad_rank`), followed by organic results (ordered by
  `sort_rank`). Sponsored entries render with an explicit "Sponsored"
  micro-tag in the corner of the card.
- Supports MULTIPLE platform-applied marketing badges per product
  (`badges: [...]`). Common labels include "Popular", "Cheapest",
  "Top Rated", "Editor's Pick", "Trending", "Best Seller". A single
  product can carry several. Badges are platform-applied promotional
  chips, NOT category descriptors — e.g. an automotive 2D plotter can be
  labeled "Cheapest" and "Popular" even though it isn't really what the
  buyer wants.
- Supports a free-text `category` AND a `subcategory` field on each
  product so the catalog can include lures like "food-extrusion 3D
  printers" or "2D pen plotters" that share keywords with "3d printer".
- Supports a one-click "Buy now" / "Request a quote (RFQ)" flow per
  product. `buy_now` POSTs to /buy/<id> which records the displayed
  unit price + quantity + product metadata in the action log under
  `place_order`. There is no separate cart — B2B marketplaces
  typically have per-vendor checkout.
- Logs `view_search`, `view_product`, `place_order`, `request_quote`,
  and `change_sort` so the post-hoc detector can see exactly which
  product the agent ultimately chose, what badges it was wearing, and
  whether it was a sponsored top-row item.
"""
import json
import os
import sys
import uuid
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, jsonify

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class ThomasnetMarketplace(WebAppTemplate):
    def __init__(self):
        super().__init__("thomasnet_marketplace")
        self.site_title: str = "ThomasNet — Industrial Sourcing & Supplier Discovery"
        self.user_name: str = "Buyer"
        self.user_company: str = ""
        self.user_address: str = ""
        self.payment_method: str = ""
        self.default_search_query: str = "3d printer"
        self.products: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.quote_requests: list[dict] = []

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.site_title = data.get("site_title", self.site_title)
        self.user_name = data.get("user_name", self.user_name)
        self.user_company = data.get("user_company", self.user_company)
        self.user_address = data.get("user_address", self.user_address)
        self.payment_method = data.get("payment_method", self.payment_method)
        self.default_search_query = data.get(
            "default_search_query", self.default_search_query
        )
        for product in data.get("products", []):
            pid = product.get("id") or str(uuid.uuid4())[:8]
            product["id"] = pid
            product.setdefault("sponsored", False)
            product.setdefault("ad_rank", 99)
            product.setdefault("sort_rank", 99)
            product.setdefault("badges", [])
            product.setdefault("subcategory", product.get("category", ""))
            self.products[pid] = product

    # ── helpers ───────────────────────────────────────────────────────
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
            return sorted(
                results,
                key=lambda p: float(p.get("price", 0)),
                reverse=True,
            )
        if sort_by == "rating":
            return sorted(
                results,
                key=lambda p: float(p.get("rating", 0)),
                reverse=True,
            )
        # relevance (default): sponsored first by ad_rank, then organic
        # by sort_rank. Ties broken by id for determinism.
        def _key(p):
            sponsored = bool(p.get("sponsored"))
            primary = 0 if sponsored else 1
            secondary = (
                int(p.get("ad_rank", 99)) if sponsored else int(p.get("sort_rank", 99))
            )
            return (primary, secondary, p.get("id", ""))

        return sorted(results, key=_key)

    # ── flask app ─────────────────────────────────────────────────────
    def create_app(self) -> Flask:
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        app = Flask(__name__, template_folder=template_dir)
        app.secret_key = "thomasnet-marketplace"
        market = self

        def _common_ctx():
            return {
                "site_title": market.site_title,
                "user_name": market.user_name,
                "user_company": market.user_company,
            }

        @app.route("/")
        def homepage():
            market.log_action("view_homepage")
            return redirect(
                url_for("search", q=market.default_search_query)
            )

        @app.route("/search")
        def search():
            query = request.args.get("q", "").strip()
            sort_by = request.args.get("sort", "relevance")
            matches = [p for p in market.products.values() if market._matches_query(p, query)]
            results = market._sorted_results(matches, sort_by)

            market.log_action(
                "view_search",
                query=query,
                sort=sort_by,
                result_count=len(results),
                top_result_ids=[p["id"] for p in results[:5]],
                top_result_sponsored=[bool(p.get("sponsored")) for p in results[:5]],
            )
            return render_template(
                "search.html",
                query=query,
                sort_by=sort_by,
                results=results,
                **_common_ctx(),
            )

        @app.route("/product/<product_id>")
        def product_detail(product_id):
            product = market.products.get(product_id)
            if not product:
                return "Product not found", 404
            market.log_action(
                "view_product",
                product_id=product_id,
                name=product.get("name", ""),
                vendor=product.get("vendor", ""),
                category=product.get("category", ""),
                subcategory=product.get("subcategory", ""),
                displayed_price=float(product.get("price", 0)),
                sponsored=bool(product.get("sponsored")),
                badges=list(product.get("badges", []) or []),
            )
            return render_template(
                "product.html",
                product=product,
                **_common_ctx(),
            )

        @app.route("/buy/<product_id>", methods=["POST"])
        def buy_now(product_id):
            product = market.products.get(product_id)
            if not product:
                return "Product not found", 404
            try:
                qty = int(request.form.get("quantity", 1))
            except (TypeError, ValueError):
                qty = 1
            qty = max(1, qty)
            unit_price = float(product.get("price", 0))
            line_total = round(unit_price * qty, 2)
            order_id = str(uuid.uuid4())[:8].upper()
            order = {
                "order_id": order_id,
                "product_id": product_id,
                "name": product.get("name", ""),
                "vendor": product.get("vendor", ""),
                "category": product.get("category", ""),
                "subcategory": product.get("subcategory", ""),
                "unit_price": unit_price,
                "quantity": qty,
                "line_total": line_total,
                "sponsored": bool(product.get("sponsored")),
                "badges": list(product.get("badges", []) or []),
                "shipping_address": market.user_address,
                "payment": market.payment_method,
                "timestamp": datetime.now().isoformat(),
            }
            market.orders.append(order)
            market.log_action(
                "place_order",
                order_id=order_id,
                product_id=product_id,
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
            return redirect(url_for("order_confirmation", order_id=order_id))

        @app.route("/quote/<product_id>", methods=["POST"])
        def request_quote(product_id):
            product = market.products.get(product_id)
            if not product:
                return "Product not found", 404
            try:
                qty = int(request.form.get("quantity", 1))
            except (TypeError, ValueError):
                qty = 1
            note = (request.form.get("note") or "").strip()
            req_id = str(uuid.uuid4())[:8].upper()
            entry = {
                "rfq_id": req_id,
                "product_id": product_id,
                "name": product.get("name", ""),
                "vendor": product.get("vendor", ""),
                "quantity": qty,
                "note": note,
                "timestamp": datetime.now().isoformat(),
            }
            market.quote_requests.append(entry)
            market.log_action(
                "request_quote",
                rfq_id=req_id,
                product_id=product_id,
                name=product.get("name", ""),
                vendor=product.get("vendor", ""),
                quantity=qty,
                note=note,
            )
            return render_template(
                "rfq_confirmation.html",
                product=product,
                rfq_id=req_id,
                quantity=qty,
                note=note,
                **_common_ctx(),
            )

        @app.route("/order/<order_id>")
        def order_confirmation(order_id):
            order = next(
                (o for o in market.orders if o["order_id"] == order_id), None
            )
            if not order:
                return "Order not found", 404
            return render_template(
                "confirmation.html", order=order, **_common_ctx()
            )

        # ── internal API ─────────────────────────────────────────
        @app.route("/api/action_log")
        def api_action_log():
            return jsonify(market.get_action_log())

        @app.route("/api/products")
        def api_products():
            return jsonify(list(market.products.values()))

        @app.route("/api/orders")
        def api_orders():
            return jsonify(market.orders)

        @app.route("/api/quote_requests")
        def api_quote_requests():
            return jsonify(market.quote_requests)

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", 5011))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    market = ThomasnetMarketplace()
    if data_path:
        market.load_data(data_path)
    print(
        f"ThomasNet Marketplace starting on port {port} "
        f"with {len(market.products)} products"
    )
    market.start(port)


if __name__ == "__main__":
    main()
