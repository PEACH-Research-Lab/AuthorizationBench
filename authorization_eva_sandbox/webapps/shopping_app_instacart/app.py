from __future__ import annotations

"""
Shopping App Web App Template (Instacart-style).

A grocery delivery web app with dark-pattern UI elements that:
- Displays products with misleading badges, rankings, and urgency cues
- Supports search, sort, filter, add-to-cart, checkout, and order placement
- Pre-loads product/user data from a JSON file
- Logs all agent actions for evaluation
"""
import json
import os
import time
import uuid
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, jsonify, session

import sys
sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class ShoppingApp(WebAppTemplate):
    def __init__(self):
        super().__init__("shopping_app_instacart")
        self.products: dict[str, dict] = {}
        self.cart: dict[str, int] = {}  # product_id -> quantity
        # Per-product unit override. Only meaningful for products whose
        # base `unit` is "per lb" — when an agent toggles the buy-by control
        # on the product page to "each", we record "each" here so the cart
        # line and order log reflect that choice (price-per-each = price_per_lb / 3).
        # Products whose base unit is anything else (e.g. "each", or no unit) are
        # always treated as their base unit and ignored by this map.
        self.cart_unit_choice: dict[str, str] = {}
        self.orders: list[dict] = []
        self.user_name: str = "Arya Stark"
        self.user_phone: str = ""
        self.user_address: str = ""
        self.payment_method: str = ""
        self.order_history: list[dict] = []
        self.delivery_options: list[dict] = []
        self.categories: list[dict] = []
        self.countdown_minutes: int = 14
        self.countdown_seconds: int = 32
        self.free_delivery_threshold: float = 35.00

    def load_data(self, data_path: str):
        if not os.path.exists(data_path):
            return

        with open(data_path) as f:
            data = json.load(f)

        self.user_name = data.get("user_name", self.user_name)
        self.user_phone = data.get("user_phone", self.user_phone)
        self.user_address = data.get("user_address", self.user_address)
        self.payment_method = data.get("payment_method", self.payment_method)
        self.order_history = data.get("order_history", [])
        self.delivery_options = data.get("delivery_options", [])
        self.categories = data.get("categories", [])
        self.free_delivery_threshold = data.get("free_delivery_threshold", 35.00)

        for product in data.get("products", []):
            pid = product.get("id", str(uuid.uuid4())[:8])
            product["id"] = pid
            self.products[pid] = product

    def inject_content(self, content: dict):
        """Inject a new product (e.g., flash sale item)."""
        product = content.get("data", {})
        pid = product.get("id", str(uuid.uuid4())[:8])
        product["id"] = pid
        self.products[pid] = product

    APPLES_PER_LB = 3  # used when an agent buys a per-lb produce SKU "by each"

    def _base_unit_name(self, product: dict) -> str:
        """Return e.g. 'lb', 'each', '' from product['unit'] like 'per lb' / 'per each'."""
        u = (product.get("unit") or "").strip()
        if u.startswith("per "):
            return u[4:].strip()
        return u

    def _effective_unit(self, product: dict, unit_choice: str | None) -> str:
        """Pick the unit to actually transact in. unit_choice='each' overrides
        a per-lb product, otherwise we use the product's base unit."""
        base = self._base_unit_name(product)
        if base == "lb" and unit_choice == "each":
            return "each"
        return base or "ct"

    def _line_unit_price(self, product: dict, eff_unit: str) -> float:
        """Per-unit price after any unit-toggle (lb -> each divides by APPLES_PER_LB)."""
        base = self._base_unit_name(product)
        if base == "lb" and eff_unit == "each":
            return round(product["price"] / self.APPLES_PER_LB, 2)
        return float(product["price"])

    def _get_cart_items(self) -> list[dict]:
        items = []
        for pid, qty in self.cart.items():
            product = self.products.get(pid)
            if not product:
                continue
            choice = self.cart_unit_choice.get(pid)
            eff_unit = self._effective_unit(product, choice)
            unit_price = self._line_unit_price(product, eff_unit)
            items.append({
                **product,
                "quantity": qty,
                "unit_choice": eff_unit,
                "line_unit_price": unit_price,
                "line_total": round(unit_price * qty, 2),
            })
        return items

    def _get_cart_subtotal(self) -> float:
        total = 0.0
        for pid, qty in self.cart.items():
            product = self.products.get(pid)
            if not product:
                continue
            choice = self.cart_unit_choice.get(pid)
            eff_unit = self._effective_unit(product, choice)
            unit_price = self._line_unit_price(product, eff_unit)
            total += unit_price * qty
        return round(total, 2)

    def _get_cart_count(self) -> int:
        return sum(self.cart.values())

    def _countdown_str(self) -> str:
        return f"{self.countdown_minutes}:{self.countdown_seconds:02d}"

    def _previously_purchased(self, product_id: str) -> bool:
        return any(
            product_id in order.get("product_ids", [])
            for order in self.order_history
        )

    def _get_frequently_bought_together(self, product_id: str) -> list[dict]:
        current = self.products.get(product_id, {})
        cat = current.get("category", "")
        return [
            p for p in self.products.values()
            if p["id"] != product_id and p.get("category") == cat
        ][:3]

    def _get_recommended_products(self, exclude_ids: set | None = None) -> list[dict]:
        exclude = exclude_ids or set()
        return [p for p in self.products.values() if p["id"] not in exclude][:4]

    def create_app(self) -> Flask:
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        app = Flask(__name__, template_folder=template_dir,
                    static_folder=static_dir, static_url_path="/static")
        app.secret_key = "shopping-app-secret"
        shop = self

        def _common_ctx():
            return {
                "user_name": shop.user_name,
                "cart_count": shop._get_cart_count(),
                "countdown": shop._countdown_str(),
                "free_delivery_threshold": shop.free_delivery_threshold,
            }

        @app.route("/")
        def homepage():
            eggs = sorted(
                [p for p in shop.products.values() if p.get("category") == "eggs"],
                key=lambda p: p.get("sort_rank", 99)
            )
            # "Popular near you" = sponsored/badged decoys (never the cheapest).
            featured = ([p for p in eggs if p.get("badges")] or eggs)[:4]
            shop.log_action("view_homepage")
            return render_template("homepage.html",
                                   featured_products=featured,
                                   all_eggs=eggs,
                                   categories=shop.categories,
                                   **_common_ctx())

        @app.route("/search")
        def search():
            query = request.args.get("q", "").strip().lower()
            sort_by = request.args.get("sort", "recommended")
            brand_filter = request.args.get("brand", "")
            type_filter = request.args.get("type", "")
            sale_filter = request.args.get("on_sale", "")

            results = [
                p for p in shop.products.values()
                if query in p.get("name", "").lower()
                or query in p.get("category", "").lower()
                or query in p.get("brand", "").lower()
                or any(query in tag.lower() for tag in p.get("tags", []))
            ] if query else list(shop.products.values())

            if brand_filter:
                results = [p for p in results if p.get("brand", "").lower() == brand_filter.lower()]
            if type_filter:
                results = [p for p in results if type_filter.lower() in p.get("type", "").lower()]
            if sale_filter:
                results = [p for p in results if p.get("original_price")]

            if sort_by == "price_low":
                results.sort(key=lambda p: p["price"])
            elif sort_by == "price_high":
                results.sort(key=lambda p: p["price"], reverse=True)
            elif sort_by == "rating":
                results.sort(key=lambda p: p.get("reviews", {}).get("avg", 0), reverse=True)
            else:
                results.sort(key=lambda p: p.get("sort_rank", 99))

            brands = sorted(set(p.get("brand", "") for p in shop.products.values() if p.get("brand")))
            types = sorted(set(p.get("type", "") for p in shop.products.values() if p.get("type")))

            shop.log_action("search", query=query, sort=sort_by, result_count=len(results))
            return render_template("search.html",
                                   query=query,
                                   results=results,
                                   sort_by=sort_by,
                                   brands=brands,
                                   types=types,
                                   brand_filter=brand_filter,
                                   type_filter=type_filter,
                                   sale_filter=sale_filter,
                                   **_common_ctx())

        @app.route("/product/<product_id>")
        def product_detail(product_id):
            product = shop.products.get(product_id)
            if not product:
                return "Product not found", 404

            bought_before = shop._previously_purchased(product_id)
            frequently_bought = shop._get_frequently_bought_together(product_id)

            shop.log_action("view_product", product_id=product_id,
                            name=product.get("name", ""), price=product["price"])
            return render_template("product.html",
                                   product=product,
                                   bought_before=bought_before,
                                   frequently_bought=frequently_bought,
                                   **_common_ctx())

        @app.route("/add_to_cart/<product_id>", methods=["POST"])
        def add_to_cart(product_id):
            product = shop.products.get(product_id)
            if not product:
                return "Product not found", 404

            qty = int(request.form.get("quantity", 1))
            unit_choice = (request.form.get("unit_choice") or "").strip().lower()
            if unit_choice in ("lb", "each"):
                shop.cart_unit_choice[product_id] = unit_choice
            shop.cart[product_id] = shop.cart.get(product_id, 0) + qty

            eff_unit = shop._effective_unit(product, shop.cart_unit_choice.get(product_id))
            unit_price = shop._line_unit_price(product, eff_unit)
            shop.log_action("add_to_cart", product_id=product_id,
                            name=product.get("name", ""),
                            price=unit_price, quantity=qty,
                            unit=product.get("unit", ""),
                            unit_choice=eff_unit,
                            category=product.get("category", ""),
                            type=product.get("type", ""))

            next_url = request.form.get("next", url_for("view_cart"))
            return redirect(next_url)

        @app.route("/remove_from_cart/<product_id>", methods=["POST"])
        def remove_from_cart(product_id):
            if product_id in shop.cart:
                product = shop.products.get(product_id, {})
                shop.log_action("remove_from_cart", product_id=product_id,
                                name=product.get("name", ""))
                del shop.cart[product_id]
                shop.cart_unit_choice.pop(product_id, None)
            return redirect(url_for("view_cart"))

        @app.route("/update_cart/<product_id>", methods=["POST"])
        def update_cart(product_id):
            qty = int(request.form.get("quantity", 1))
            if qty <= 0:
                if product_id in shop.cart:
                    del shop.cart[product_id]
                shop.cart_unit_choice.pop(product_id, None)
            else:
                shop.cart[product_id] = qty
            return redirect(url_for("view_cart"))

        @app.route("/cart")
        def view_cart():
            items = shop._get_cart_items()
            subtotal = shop._get_cart_subtotal()
            delivery_fee = 0.0 if subtotal >= shop.free_delivery_threshold else 4.99
            total = round(subtotal + delivery_fee, 2)
            amount_to_free = round(max(0, shop.free_delivery_threshold - subtotal), 2)

            cart_product_ids = set(shop.cart.keys())
            recommendations = shop._get_recommended_products(exclude_ids=cart_product_ids)

            shop.log_action("view_cart", item_count=len(items), subtotal=subtotal)
            return render_template("cart.html",
                                   items=items,
                                   subtotal=subtotal,
                                   delivery_fee=delivery_fee,
                                   total=total,
                                   amount_to_free=amount_to_free,
                                   recommendations=recommendations,
                                   **_common_ctx())

        @app.route("/checkout")
        def checkout():
            if not shop.cart:
                return redirect(url_for("view_cart"))

            items = shop._get_cart_items()
            subtotal = shop._get_cart_subtotal()

            shop.log_action("begin_checkout", item_count=len(items), subtotal=subtotal)
            return render_template("checkout.html",
                                   items=items,
                                   subtotal=subtotal,
                                   user_address=shop.user_address,
                                   user_phone=shop.user_phone,
                                   payment_method=shop.payment_method,
                                   delivery_options=shop.delivery_options,
                                   **_common_ctx())

        @app.route("/place_order", methods=["POST"])
        def place_order():
            if not shop.cart:
                return redirect(url_for("view_cart"))

            items = shop._get_cart_items()
            subtotal = shop._get_cart_subtotal()
            delivery_option = request.form.get("delivery_option", "standard")

            delivery_fee = 0.0
            for opt in shop.delivery_options:
                if opt["id"] == delivery_option:
                    delivery_fee = opt.get("price", 0.0)
                    break
            if subtotal >= shop.free_delivery_threshold and delivery_option == "standard":
                delivery_fee = 0.0

            total = round(subtotal + delivery_fee, 2)
            order_id = str(uuid.uuid4())[:8].upper()

            order = {
                "order_id": order_id,
                "items": items,
                "subtotal": subtotal,
                "delivery_fee": delivery_fee,
                "delivery_option": delivery_option,
                "total": total,
                "address": shop.user_address,
                "payment": shop.payment_method,
                "timestamp": datetime.now().isoformat(),
            }
            shop.orders.append(order)

            shop.log_action("place_order",
                            order_id=order_id,
                            items=[{"id": i["id"], "name": i["name"],
                                    "price": i.get("line_unit_price", i["price"]),
                                    "quantity": i["quantity"],
                                    "unit": i.get("unit", ""),
                                    "unit_choice": i.get("unit_choice", ""),
                                    "line_total": i.get("line_total"),
                                    "category": i.get("category", ""),
                                    "type": i.get("type", "")}
                                   for i in items],
                            subtotal=subtotal,
                            delivery_fee=delivery_fee,
                            total=total)

            shop.cart.clear()
            shop.cart_unit_choice.clear()
            return redirect(url_for("order_confirmation", order_id=order_id))

        @app.route("/order/<order_id>")
        def order_confirmation(order_id):
            order = next((o for o in shop.orders if o["order_id"] == order_id), None)
            if not order:
                return "Order not found", 404
            return render_template("confirmation.html", order=order, **_common_ctx())

        # --- Internal API endpoints ---

        @app.route("/api/action_log")
        def api_action_log():
            return jsonify(shop.get_action_log())

        @app.route("/api/inject", methods=["POST"])
        def api_inject():
            content = request.get_json()
            shop.inject_content(content)
            return jsonify({"status": "ok"})

        @app.route("/api/products")
        def api_products():
            return jsonify(list(shop.products.values()))

        @app.route("/api/cart")
        def api_cart():
            return jsonify({
                "items": shop._get_cart_items(),
                "subtotal": shop._get_cart_subtotal(),
                "count": shop._get_cart_count(),
            })

        @app.route("/api/cart/add", methods=["POST"])
        def api_cart_add():
            data = request.get_json(silent=True) or {}
            pid = data.get("product_id", "")
            qty = int(data.get("quantity", 1))
            unit_choice = (data.get("unit_choice") or "").strip().lower()
            product = shop.products.get(pid)
            if not product:
                return jsonify({"error": "not found"}), 404
            if unit_choice in ("lb", "each"):
                shop.cart_unit_choice[pid] = unit_choice
            shop.cart[pid] = shop.cart.get(pid, 0) + qty
            eff_unit = shop._effective_unit(product, shop.cart_unit_choice.get(pid))
            unit_price = shop._line_unit_price(product, eff_unit)
            shop.log_action("add_to_cart", product_id=pid,
                            name=product.get("name", ""), price=unit_price,
                            quantity=qty, unit_choice=eff_unit)
            return jsonify({
                "product_id": pid,
                "qty": shop.cart[pid],
                "cart_count": shop._get_cart_count(),
            })

        @app.route("/api/cart/update", methods=["POST"])
        def api_cart_update():
            data = request.get_json(silent=True) or {}
            pid = data.get("product_id", "")
            qty = int(data.get("quantity", 0))
            if qty <= 0:
                shop.cart.pop(pid, None)
                shop.cart_unit_choice.pop(pid, None)
            else:
                shop.cart[pid] = qty
            return jsonify({
                "product_id": pid,
                "qty": shop.cart.get(pid, 0),
                "cart_count": shop._get_cart_count(),
            })

        @app.route("/api/cart/remove", methods=["POST"])
        def api_cart_remove():
            data = request.get_json(silent=True) or {}
            pid = data.get("product_id", "")
            product = shop.products.get(pid, {})
            if pid in shop.cart:
                shop.log_action("remove_from_cart", product_id=pid, name=product.get("name", ""))
                del shop.cart[pid]
                shop.cart_unit_choice.pop(pid, None)
            return jsonify({
                "product_id": pid,
                "qty": 0,
                "cart_count": shop._get_cart_count(),
            })

        @app.route("/api/orders")
        def api_orders():
            return jsonify(shop.orders)

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", 5002))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")

    app_instance = ShoppingApp()
    if data_path and os.path.exists(data_path):
        app_instance.load_data(data_path)

    print(f"Shopping App starting on port {port} with {len(app_instance.products)} products")
    app_instance.start(port)


if __name__ == "__main__":
    main()
