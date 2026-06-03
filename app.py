"""
dt-port-demo (shopdemo) — a tiny e-commerce-shaped Flask app
for Datadog + Port MCP observability demos.

Endpoints worth tracing:
  GET  /                     -> HTML home page (product grid)
  GET  /products             -> JSON product list
  GET  /products/<sku>       -> JSON product detail (random ~50ms work + 1% miss)
  POST /cart/add             -> add product to session cart
  GET  /cart                 -> view current cart
  POST /cart/clear           -> empty the cart
  POST /checkout             -> create order (~5% intentional payment failure)
  GET  /orders               -> list all orders (HTML)
  GET  /orders/<order_id>    -> order detail (HTML)
  GET  /shipping/rates       -> outbound call to api.github.com (proxy for "rates API")

Legacy demo endpoints kept for continuity:
  GET  /healthz              -> liveness
  GET  /slow                 -> always slow
  GET  /error                -> always 500
"""

# ---- Load .env FIRST before anything else -----------------------------------
from dotenv import load_dotenv
load_dotenv()

# ---- Datadog tracing — must come before all other imports -------------------
from ddtrace import patch_all
patch_all()

# ---- Standard imports -------------------------------------------------------
import logging
import os
import random
import time
import uuid
from threading import Lock

import requests
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-do-not-use-in-prod")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shopdemo")

UPSTREAM_RATES_URL = os.getenv("UPSTREAM_RATES_URL", "https://api.github.com/zen")

# --- Seed product catalog (in-memory) ----------------------------------------

PRODUCTS = {
    "SKU-001": {"sku": "SKU-001", "name": "Mechanical Keyboard",        "price": 129.00, "stock": 14, "category": "Peripherals"},
    "SKU-002": {"sku": "SKU-002", "name": "Wireless Mouse",              "price":  49.00, "stock": 32, "category": "Peripherals"},
    "SKU-003": {"sku": "SKU-003", "name": "27'' 4K Monitor",             "price": 449.00, "stock":  7, "category": "Displays"},
    "SKU-004": {"sku": "SKU-004", "name": "USB-C Hub (8-in-1)",          "price":  59.00, "stock": 48, "category": "Accessories"},
    "SKU-005": {"sku": "SKU-005", "name": "Noise-Cancelling Headphones", "price": 299.00, "stock": 11, "category": "Audio"},
    "SKU-006": {"sku": "SKU-006", "name": "Ergonomic Office Chair",      "price": 619.00, "stock":  4, "category": "Furniture"},
    "SKU-007": {"sku": "SKU-007", "name": "Standing Desk (1.4m)",        "price": 729.00, "stock":  3, "category": "Furniture"},
    "SKU-008": {"sku": "SKU-008", "name": "Webcam 1080p",                "price":  89.00, "stock": 22, "category": "Peripherals"},
}

ORDERS: dict[str, dict] = {}
_orders_lock = Lock()

# --- Helpers -----------------------------------------------------------------

def _get_cart() -> dict:
    """Cart lives in flask session; returns dict {sku: qty}."""
    cart = session.get("cart")
    if cart is None:
        cart = {}
        session["cart"] = cart
    return cart


def _cart_lines(cart: dict) -> list[dict]:
    """Expand cart {sku: qty} into list of {product, qty, line_total}."""
    lines = []
    for sku, qty in cart.items():
        product = PRODUCTS.get(sku)
        if not product:
            continue
        lines.append({
            "product": product,
            "qty": qty,
            "line_total": round(product["price"] * qty, 2),
        })
    return lines


def _cart_total(cart: dict) -> float:
    return round(sum(l["line_total"] for l in _cart_lines(cart)), 2)


# --- Routes ------------------------------------------------------------------

@app.route("/")
def home():
    return render_template(
        "home.html",
        products=list(PRODUCTS.values()),
        cart_count=sum(_get_cart().values()),
    )


@app.route("/products")
def products_list():
    return jsonify(list(PRODUCTS.values()))


@app.route("/products/<sku>")
def product_detail(sku):
    # 1% simulated cache miss / DB blip
    if random.random() < 0.01:
        log.warning("product_detail: simulated lookup blip for %s", sku)
        time.sleep(random.uniform(0.3, 0.7))
    # gentle baseline latency so /products/* spans look realistic
    time.sleep(random.uniform(0.02, 0.08))
    product = PRODUCTS.get(sku)
    if not product:
        abort(404, description=f"unknown sku {sku}")
    return jsonify(product)


@app.route("/cart/add", methods=["POST"])
def cart_add():
    sku = request.form.get("sku") or (request.json or {}).get("sku")
    qty = int(request.form.get("qty") or (request.json or {}).get("qty") or 1)
    if sku not in PRODUCTS:
        abort(400, description=f"unknown sku {sku}")
    cart = _get_cart()
    cart[sku] = cart.get(sku, 0) + qty
    session.modified = True
    log.info("cart_add: sku=%s qty=%s total_items=%s", sku, qty, sum(cart.values()))
    if request.form:
        return redirect(url_for("cart_view"))
    return jsonify(cart=cart, item_count=sum(cart.values()))


@app.route("/cart")
def cart_view():
    cart = _get_cart()
    lines = _cart_lines(cart)
    return render_template("cart.html", lines=lines, total=_cart_total(cart))


@app.route("/cart/clear", methods=["POST"])
def cart_clear():
    session["cart"] = {}
    session.modified = True
    return redirect(url_for("cart_view"))


@app.route("/checkout", methods=["POST"])
def checkout():
    """Creates an order. ~5% intentional payment failures for the demo."""
    cart = _get_cart()
    if not cart:
        abort(400, description="cart is empty")
    lines = _cart_lines(cart)
    total = _cart_total(cart)

    # simulated payment processing latency
    time.sleep(random.uniform(0.2, 0.6))

    # ~5% intentional failure — gives Datadog a meaningful checkout failure rate
    if random.random() < 0.05:
        log.error("checkout: simulated payment failure (total=%.2f)", total)
        abort(502, description="payment gateway error (simulated)")

    order_id = "ORD-" + uuid.uuid4().hex[:8].upper()
    order = {
        "id": order_id,
        "lines": [
            {
                "sku": l["product"]["sku"],
                "name": l["product"]["name"],
                "qty": l["qty"],
                "line_total": l["line_total"],
            }
            for l in lines
        ],
        "total": total,
        "status": "confirmed",
    }
    with _orders_lock:
        ORDERS[order_id] = order
    session["cart"] = {}
    session.modified = True
    log.info("checkout: order=%s total=%.2f items=%s", order_id, total, sum(cart.values()))
    if request.form:
        return redirect(url_for("order_detail", order_id=order_id))
    return jsonify(order), 201


@app.route("/orders")
def orders_list():
    with _orders_lock:
        all_orders = list(ORDERS.values())
    return render_template("orders.html", orders=all_orders)


@app.route("/orders/<order_id>")
def order_detail(order_id):
    with _orders_lock:
        order = ORDERS.get(order_id)
    if not order:
        abort(404, description=f"unknown order {order_id}")
    return render_template("order_detail.html", order=order)


@app.route("/shipping/rates")
def shipping_rates():
    """Outbound HTTP call — Datadog shows this as a child span."""
    try:
        r = requests.get(UPSTREAM_RATES_URL, timeout=3)
        return jsonify(
            provider="github-zen (mock rates)",
            status=r.status_code,
            sample=r.text[:80],
        )
    except requests.RequestException as exc:
        log.warning("shipping_rates: upstream failed: %s", exc)
        abort(503, description="rates provider unavailable")


# --- Legacy demo endpoints ---------------------------------------------------

@app.route("/healthz")
def healthz():
    return jsonify(status="ok"), 200


@app.route("/slow")
def slow():
    delay = random.uniform(1.5, 3.0)
    time.sleep(delay)
    return jsonify(slept=round(delay, 2))


@app.route("/error")
def error():
    raise RuntimeError("intentional failure for demo")


# --- Error handlers ----------------------------------------------------------

@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(502)
@app.errorhandler(503)
def _http_error(e):
    code = getattr(e, "code", 500)
    desc = getattr(e, "description", str(e))
    if (
        request.accept_mimetypes.best == "application/json"
        or request.path.startswith(("/products", "/cart/add", "/shipping"))
    ):
        return jsonify(error=desc, status=code), code
    return render_template("error.html", code=code, description=desc), code


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        debug=False,
    )