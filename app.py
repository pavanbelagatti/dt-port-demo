"""
dt-port-demo (shopdemo) — e-commerce Flask app for Datadog + Port demos.

Core endpoints:
  GET  /                     -> home page
  GET  /products             -> product list
  GET  /products/<sku>       -> product detail
  POST /cart/add             -> add to cart
  GET  /cart                 -> view cart
  POST /cart/clear           -> clear cart
  POST /checkout             -> create order (~5% payment failure)
  GET  /orders               -> order list
  GET  /orders/<order_id>    -> order detail
  GET  /shipping/rates       -> outbound call to api.github.com

Service endpoints (each emits APM under its own service name):
  GET  /payments/process     -> service: payments-service (~8% failure)
  GET  /auth/verify          -> service: auth-service (~3% failure)
  GET  /shipping/track/<id>  -> service: shipping-service (~5% failure)
  GET  /inventory/check      -> service: inventory-service (fast, rarely fails)

Failure scenario endpoints:
  GET  /scenarios/memory-leak      -> simulates unbounded memory growth
  GET  /scenarios/slow-db          -> simulates slow DB query (2-8s)
  GET  /scenarios/cascade-failure  -> cascade: payments -> shipping -> orders
  GET  /scenarios/404-spike        -> generates burst of 404s
  GET  /scenarios/auth-failure     -> simulates auth service degradation
  GET  /scenarios/latency-spike    -> sudden latency spike across endpoints
  GET  /scenarios/reset-all        -> reset all failure modes
  GET  /scenarios/status           -> check active failure modes

Legacy:
  GET  /healthz  -> liveness
  GET  /slow     -> always slow
  GET  /error    -> always 500
"""

# ---- Load .env FIRST ---------------------------------------------------------
from dotenv import load_dotenv
load_dotenv()

# ---- Datadog tracing — must come before all other imports --------------------
from ddtrace import patch_all, tracer
patch_all()

# ---- Standard imports --------------------------------------------------------
import gc
import logging
import os
import random
import time
import uuid
from threading import Lock

import requests
from flask import (
    Flask, abort, jsonify, redirect,
    render_template, request, session, url_for,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-do-not-use-in-prod")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shopdemo")

UPSTREAM_RATES_URL = os.getenv("UPSTREAM_RATES_URL", "https://api.github.com/zen")

# --- Global failure mode flags ------------------------------------------------
_failure_modes = {
    "memory_leak":     False,
    "slow_db":         False,
    "cascade_failure": False,
    "auth_degraded":   False,
    "latency_spike":   False,
}
_failure_lock = Lock()
_leak_bucket = []

# --- Seed product catalog -----------------------------------------------------
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

# --- Helpers ------------------------------------------------------------------

def _get_cart() -> dict:
    cart = session.get("cart")
    if cart is None:
        cart = {}
        session["cart"] = cart
    return cart

def _cart_lines(cart: dict) -> list[dict]:
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

def _extra_latency(base_min=0.0, base_max=0.1) -> float:
    base = random.uniform(base_min, base_max)
    if _failure_modes["latency_spike"]:
        base += random.uniform(1.5, 4.0)
    return base

# --- Core routes --------------------------------------------------------------

@app.route("/")
def home():
    return render_template(
        "home.html",
        products=list(PRODUCTS.values()),
        cart_count=sum(_get_cart().values()),
    )

@app.route("/products")
def products_list():
    time.sleep(_extra_latency(0.01, 0.05))
    return jsonify(list(PRODUCTS.values()))

@app.route("/products/<sku>")
def product_detail(sku):
    if random.random() < 0.01:
        log.warning("product_detail: simulated lookup blip for %s", sku)
        time.sleep(random.uniform(0.3, 0.7))
    time.sleep(_extra_latency(0.02, 0.08))
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
    """~5% intentional payment failures, spikes during cascade."""
    cart = _get_cart()
    if not cart:
        abort(400, description="cart is empty")
    lines = _cart_lines(cart)
    total = _cart_total(cart)

    time.sleep(_extra_latency(0.2, 0.6))

    failure_rate = 0.60 if _failure_modes["cascade_failure"] else 0.05
    if random.random() < failure_rate:
        log.error("checkout: payment failure (cascade=%s, total=%.2f)",
                  _failure_modes["cascade_failure"], total)
        abort(502, description="payment gateway error (simulated)")

    order_id = "ORD-" + uuid.uuid4().hex[:8].upper()
    order = {
        "id": order_id,
        "lines": [{"sku": l["product"]["sku"], "name": l["product"]["name"],
                   "qty": l["qty"], "line_total": l["line_total"]} for l in lines],
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
    try:
        r = requests.get(UPSTREAM_RATES_URL, timeout=3)
        return jsonify(provider="github-zen (mock rates)", status=r.status_code, sample=r.text[:80])
    except requests.RequestException as exc:
        log.warning("shipping_rates: upstream failed: %s", exc)
        abort(503, description="rates provider unavailable")

# --- Service endpoints — each emits APM under its own service name ------------

@app.route("/payments/process")
def payments_process():
    """
    Emits traces as service: payments-service.
    8% failure rate, higher during cascade failure.
    Owner: Payments team · Tier-1
    """
    with tracer.trace("payments.process",
                      service="payments-service",
                      resource="GET /payments/process") as span:

        latency = _extra_latency(0.1, 0.4)
        if _failure_modes["cascade_failure"]:
            latency += random.uniform(2.0, 5.0)
        time.sleep(latency)

        failure_rate = 0.70 if _failure_modes["cascade_failure"] else 0.08
        if random.random() < failure_rate:
            span.set_tag("error", True)
            span.set_tag("error.msg", "payments service unavailable")
            log.error("payments: processing failure (cascade=%s)", _failure_modes["cascade_failure"])
            abort(503, description="payments service unavailable")

        amount = round(random.uniform(10, 500), 2)
        span.set_tag("payment.amount", amount)
        span.set_tag("payment.status", "approved")
        log.info("payments: processed amount=%.2f latency=%.3fs", amount, latency)
        return jsonify(status="approved", amount=amount, latency_ms=round(latency * 1000))


@app.route("/auth/verify")
def auth_verify():
    """
    Emits traces as service: auth-service.
    3% failure rate, spikes to 45% when auth_degraded is active.
    Owner: Security team · Tier-1
    """
    with tracer.trace("auth.verify",
                      service="auth-service",
                      resource="GET /auth/verify") as span:

        latency = _extra_latency(0.05, 0.15)
        if _failure_modes["auth_degraded"]:
            latency += random.uniform(1.0, 3.0)
        time.sleep(latency)

        failure_rate = 0.45 if _failure_modes["auth_degraded"] else 0.03
        if random.random() < failure_rate:
            span.set_tag("error", True)
            span.set_tag("error.msg", "token validation failed")
            log.error("auth: verification failure (degraded=%s)", _failure_modes["auth_degraded"])
            abort(401, description="auth service degraded — token validation failed")

        user_id = "usr-" + uuid.uuid4().hex[:8]
        span.set_tag("auth.user_id", user_id)
        span.set_tag("auth.status", "verified")
        log.info("auth: verified user=%s latency=%.3fs", user_id, latency)
        return jsonify(status="verified", user_id=user_id, latency_ms=round(latency * 1000))


@app.route("/shipping/track/<tracking_id>")
def shipping_track(tracking_id):
    """
    Emits traces as service: shipping-service.
    5% failure rate, spikes during cascade failure.
    Owner: Logistics team · Tier-2
    """
    with tracer.trace("shipping.track",
                      service="shipping-service",
                      resource="GET /shipping/track") as span:

        latency = _extra_latency(0.1, 0.3)
        if _failure_modes["cascade_failure"]:
            latency += random.uniform(1.5, 4.0)
        time.sleep(latency)

        failure_rate = 0.55 if _failure_modes["cascade_failure"] else 0.05
        if random.random() < failure_rate:
            span.set_tag("error", True)
            span.set_tag("error.msg", "shipping provider timeout")
            log.error("shipping: tracking failure for %s", tracking_id)
            abort(503, description="shipping provider timeout")

        statuses = ["in_transit", "out_for_delivery", "delivered", "processing"]
        status = random.choice(statuses)
        span.set_tag("shipping.tracking_id", tracking_id)
        span.set_tag("shipping.status", status)
        log.info("shipping: tracked id=%s status=%s latency=%.3fs", tracking_id, status, latency)
        return jsonify(tracking_id=tracking_id, status=status, latency_ms=round(latency * 1000))


@app.route("/inventory/check")
def inventory_check():
    """
    Emits traces as service: inventory-service.
    Fast, rarely fails.
    Owner: Platform team · Tier-2
    """
    with tracer.trace("inventory.check",
                      service="inventory-service",
                      resource="GET /inventory/check") as span:

        time.sleep(_extra_latency(0.01, 0.05))
        sku = random.choice(list(PRODUCTS.keys()))
        product = PRODUCTS[sku]
        span.set_tag("inventory.sku", sku)
        span.set_tag("inventory.stock", product["stock"])
        return jsonify(sku=sku, name=product["name"],
                       stock=product["stock"], available=product["stock"] > 0)


# --- Failure scenario endpoints -----------------------------------------------

@app.route("/scenarios/memory-leak")
def scenario_memory_leak():
    """
    Scenario: Memory leak — unbounded object accumulation.
    Owner: Platform · Tier-1
    """
    global _leak_bucket
    with _failure_lock:
        _failure_modes["memory_leak"] = True

    chunk = ["x" * 1024 for _ in range(1024)]
    _leak_bucket.extend(chunk)
    leak_size_mb = round(len(_leak_bucket) / (1024 * 1024), 2)
    log.warning("memory_leak: bucket ~%.2fMB (%d objects)", leak_size_mb, len(_leak_bucket))

    return jsonify(
        scenario="memory-leak", status="active",
        leak_size_mb=leak_size_mb, objects_allocated=len(_leak_bucket),
        symptom="heap growing unboundedly — GC pressure will increase",
        owner="Platform",
        runbook="Check for unbounded caches, session objects, or event listeners not being cleared"
    )

@app.route("/scenarios/memory-leak/reset")
def scenario_memory_leak_reset():
    global _leak_bucket
    _leak_bucket = []
    gc.collect()
    with _failure_lock:
        _failure_modes["memory_leak"] = False
    return jsonify(scenario="memory-leak", status="reset")

@app.route("/scenarios/slow-db")
def scenario_slow_db():
    """
    Scenario: Slow DB query — missing index causes full table scan.
    Owner: Payments · Tier-1
    """
    with _failure_lock:
        _failure_modes["slow_db"] = True

    query_time = random.uniform(2.0, 8.0)
    time.sleep(query_time)
    log.warning("slow_db: query took %.2fs (simulated missing index)", query_time)

    return jsonify(
        scenario="slow-db", status="active",
        query_time_seconds=round(query_time, 2),
        symptom=f"SELECT on orders table took {round(query_time, 2)}s — likely missing index",
        owner="Payments",
        runbook="Check slow query log · Run EXPLAIN on orders queries · Add index on created_at",
        affected_endpoints=["/checkout", "/orders", "/payments/process"]
    )

@app.route("/scenarios/slow-db/reset")
def scenario_slow_db_reset():
    with _failure_lock:
        _failure_modes["slow_db"] = False
    return jsonify(scenario="slow-db", status="reset")

@app.route("/scenarios/cascade-failure")
def scenario_cascade_failure():
    """
    Scenario: Cascade failure — payments → checkout → shipping.
    Owner: Platform · Tier-1
    """
    with _failure_lock:
        _failure_modes["cascade_failure"] = True

    log.error("cascade_failure: ACTIVATED — payments degraded, cascading to checkout and shipping")
    return jsonify(
        scenario="cascade-failure", status="ACTIVE",
        symptom="payments-service degraded → checkout 60% failure → shipping timeouts",
        cascade_path=["payments-service → 503 (70%)", "checkout → 502 (60%)", "shipping/track → 503 (55%)"],
        owner="Platform",
        runbook="1. Check payments-service · 2. Enable circuit breaker · 3. Route to fallback queue",
        affected_services=["dt-port-demo", "payments-service", "shipping-service"]
    )

@app.route("/scenarios/cascade-failure/reset")
def scenario_cascade_failure_reset():
    with _failure_lock:
        _failure_modes["cascade_failure"] = False
    return jsonify(scenario="cascade-failure", status="reset")

@app.route("/scenarios/404-spike")
def scenario_404_spike():
    """
    Scenario: 404 spike — bad deploy broke product URL routing.
    Owner: Platform · Tier-2
    """
    bad_skus = [f"SKU-{random.randint(900, 999):03d}" for _ in range(5)]
    errors = []
    for sku in bad_skus:
        log.error("404_spike: request for non-existent sku=%s", sku)
        errors.append({"sku": sku, "status": 404, "error": f"unknown sku {sku}"})

    return jsonify(
        scenario="404-spike", status="active",
        errors_generated=len(errors),
        symptom="surge of 404s on /products/<sku> — likely bad deploy broke URL routing",
        owner="Platform",
        runbook="Check recent deploy diff · Verify product SKU migration ran · Check CDN routing rules",
        sample_errors=errors
    )

@app.route("/scenarios/auth-failure")
def scenario_auth_failure():
    """
    Scenario: Auth service degradation.
    Owner: Security · Tier-1
    """
    with _failure_lock:
        _failure_modes["auth_degraded"] = True

    log.error("auth_failure: ACTIVATED — token validation degraded, 45%% failure rate")
    return jsonify(
        scenario="auth-failure", status="ACTIVE",
        symptom="auth-service token validation failing at 45% rate with 1-3s latency",
        owner="Security",
        runbook="1. Check JWT signing service · 2. Verify token cache hit rate · 3. Restart auth pods",
        affected_endpoints=["/auth/verify", "/checkout", "/orders"]
    )

@app.route("/scenarios/auth-failure/reset")
def scenario_auth_failure_reset():
    with _failure_lock:
        _failure_modes["auth_degraded"] = False
    return jsonify(scenario="auth-failure", status="reset")

@app.route("/scenarios/latency-spike")
def scenario_latency_spike():
    """
    Scenario: Global latency spike — resource contention.
    Owner: Platform · Tier-1
    """
    with _failure_lock:
        _failure_modes["latency_spike"] = True

    log.error("latency_spike: ACTIVATED — adding 1.5-4s to all endpoint response times")
    return jsonify(
        scenario="latency-spike", status="ACTIVE",
        symptom="p99 latency spiking 1.5-4s across ALL endpoints — likely resource contention",
        owner="Platform",
        runbook="1. Check CPU/memory · 2. Look for noisy neighbour · 3. Check DB connection pool",
        affected_endpoints="ALL"
    )

@app.route("/scenarios/latency-spike/reset")
def scenario_latency_spike_reset():
    with _failure_lock:
        _failure_modes["latency_spike"] = False
    return jsonify(scenario="latency-spike", status="reset")

@app.route("/scenarios/reset-all")
def scenario_reset_all():
    global _leak_bucket
    with _failure_lock:
        for k in _failure_modes:
            _failure_modes[k] = False
    _leak_bucket = []
    gc.collect()
    log.info("scenarios: ALL failure modes reset")
    return jsonify(status="all scenarios reset", failure_modes=_failure_modes)

@app.route("/scenarios/status")
def scenario_status():
    return jsonify(
        failure_modes=_failure_modes,
        leak_bucket_size=len(_leak_bucket),
        active_scenarios=[k for k, v in _failure_modes.items() if v]
    )

# --- Legacy demo endpoints ----------------------------------------------------

@app.route("/healthz")
def healthz():
    active = [k for k, v in _failure_modes.items() if v]
    return jsonify(status="degraded" if active else "ok", active_failure_modes=active), 200

@app.route("/slow")
def slow():
    delay = random.uniform(1.5, 3.0)
    time.sleep(delay)
    return jsonify(slept=round(delay, 2))

@app.route("/error")
def error():
    raise RuntimeError("intentional failure for demo")

# --- Error handlers -----------------------------------------------------------

@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(404)
@app.errorhandler(502)
@app.errorhandler(503)
def _http_error(e):
    code = getattr(e, "code", 500)
    desc = getattr(e, "description", str(e))
    if (
        request.accept_mimetypes.best == "application/json"
        or request.path.startswith(("/products", "/cart/add", "/shipping",
                                    "/payments", "/auth", "/inventory", "/scenarios"))
    ):
        return jsonify(error=desc, status=code), code
    return render_template("error.html", code=code, description=desc), code


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        debug=False,
    )