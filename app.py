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

Service endpoints (simulate microservice calls):
  GET  /payments/process     -> payments-service (~8% failure, variable latency)
  GET  /auth/verify          -> auth-service (~3% failure, session validation)
  GET  /shipping/track/<id>  -> shipping-service (~5% failure, downstream delay)
  GET  /inventory/check      -> inventory-service (fast, rarely fails)

Failure scenario endpoints (for realistic incident demos):
  GET  /scenarios/memory-leak      -> simulates unbounded memory growth
  GET  /scenarios/slow-db          -> simulates slow DB query (2-8s)
  GET  /scenarios/cascade-failure  -> simulates cascade: payments -> shipping -> orders
  GET  /scenarios/404-spike        -> generates burst of 404s
  GET  /scenarios/auth-failure     -> simulates auth service degradation
  GET  /scenarios/latency-spike    -> simulates sudden latency spike across endpoints

Legacy:
  GET  /healthz  -> liveness
  GET  /slow     -> always slow
  GET  /error    -> always 500
"""

# ---- Load .env FIRST ---------------------------------------------------------
from dotenv import load_dotenv
load_dotenv()

# ---- Datadog tracing — must come before all other imports --------------------
from ddtrace import patch_all
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

# --- Global failure mode flags (toggled by scenario endpoints) ----------------
_failure_modes = {
    "memory_leak":     False,
    "slow_db":         False,
    "cascade_failure": False,
    "auth_degraded":   False,
    "latency_spike":   False,
}
_failure_lock = Lock()

# Simulated memory leak bucket
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
    """Add extra latency if latency spike mode is active."""
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
    """~5% intentional payment failures."""
    cart = _get_cart()
    if not cart:
        abort(400, description="cart is empty")
    lines = _cart_lines(cart)
    total = _cart_total(cart)

    time.sleep(_extra_latency(0.2, 0.6))

    # Cascade failure makes checkout fail much more
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

# --- Service endpoints (simulate microservice calls) --------------------------

@app.route("/payments/process")
def payments_process():
    """payments-service — 8% failure rate, variable latency."""
    latency = _extra_latency(0.1, 0.4)
    if _failure_modes["cascade_failure"]:
        latency += random.uniform(2.0, 5.0)
    time.sleep(latency)

    failure_rate = 0.70 if _failure_modes["cascade_failure"] else 0.08
    if random.random() < failure_rate:
        log.error("payments: processing failure (cascade=%s)", _failure_modes["cascade_failure"])
        abort(503, description="payments service unavailable")

    amount = round(random.uniform(10, 500), 2)
    log.info("payments: processed amount=%.2f latency=%.3fs", amount, latency)
    return jsonify(status="approved", amount=amount, latency_ms=round(latency * 1000))

@app.route("/auth/verify")
def auth_verify():
    """auth-service — 3% failure, spikes when auth_degraded is active."""
    latency = _extra_latency(0.05, 0.15)
    if _failure_modes["auth_degraded"]:
        latency += random.uniform(1.0, 3.0)
    time.sleep(latency)

    failure_rate = 0.45 if _failure_modes["auth_degraded"] else 0.03
    if random.random() < failure_rate:
        log.error("auth: verification failure (degraded=%s)", _failure_modes["auth_degraded"])
        abort(401, description="auth service degraded — token validation failed")

    user_id = "usr-" + uuid.uuid4().hex[:8]
    log.info("auth: verified user=%s latency=%.3fs", user_id, latency)
    return jsonify(status="verified", user_id=user_id, latency_ms=round(latency * 1000))

@app.route("/shipping/track/<tracking_id>")
def shipping_track(tracking_id):
    """shipping-service — 5% failure, downstream delay."""
    latency = _extra_latency(0.1, 0.3)
    if _failure_modes["cascade_failure"]:
        latency += random.uniform(1.5, 4.0)
    time.sleep(latency)

    failure_rate = 0.55 if _failure_modes["cascade_failure"] else 0.05
    if random.random() < failure_rate:
        log.error("shipping: tracking failure for %s", tracking_id)
        abort(503, description="shipping provider timeout")

    statuses = ["in_transit", "out_for_delivery", "delivered", "processing"]
    log.info("shipping: tracked id=%s latency=%.3fs", tracking_id, latency)
    return jsonify(tracking_id=tracking_id, status=random.choice(statuses), latency_ms=round(latency * 1000))

@app.route("/inventory/check")
def inventory_check():
    """inventory-service — fast, rarely fails."""
    time.sleep(_extra_latency(0.01, 0.05))
    sku = random.choice(list(PRODUCTS.keys()))
    product = PRODUCTS[sku]
    return jsonify(sku=sku, name=product["name"], stock=product["stock"], available=product["stock"] > 0)

# --- Failure scenario endpoints -----------------------------------------------

@app.route("/scenarios/memory-leak")
def scenario_memory_leak():
    """
    Scenario: Memory leak — unbounded object accumulation.
    Simulates a service that allocates memory but never frees it.
    In production: look for growing heap, GC pressure, eventual OOM.
    Owner: Platform team · Tier-1 · Runbook: check for unbounded caches
    """
    global _leak_bucket
    with _failure_lock:
        _failure_modes["memory_leak"] = True

    # Allocate ~1MB of data per call, never free it
    chunk = ["x" * 1024 for _ in range(1024)]
    _leak_bucket.extend(chunk)

    leak_size_mb = round(len(_leak_bucket) / (1024 * 1024), 2)
    log.warning("memory_leak: bucket size ~%.2fMB (%d objects)", leak_size_mb, len(_leak_bucket))

    return jsonify(
        scenario="memory-leak",
        status="active",
        leak_size_mb=leak_size_mb,
        objects_allocated=len(_leak_bucket),
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
    log.info("memory_leak: reset — bucket cleared")
    return jsonify(scenario="memory-leak", status="reset", objects_freed=True)

@app.route("/scenarios/slow-db")
def scenario_slow_db():
    """
    Scenario: Slow DB query — missing index causes full table scan.
    Simulates a DB query that takes 2-8 seconds.
    In production: look for p99 latency spike, connection pool exhaustion.
    Owner: Payments team · Tier-1 · Runbook: check slow query log, add index
    """
    with _failure_lock:
        _failure_modes["slow_db"] = True

    # Simulate the slow query
    query_time = random.uniform(2.0, 8.0)
    time.sleep(query_time)

    log.warning("slow_db: query took %.2fs (simulated missing index on orders table)", query_time)
    return jsonify(
        scenario="slow-db",
        status="active",
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
    log.info("slow_db: reset")
    return jsonify(scenario="slow-db", status="reset")

@app.route("/scenarios/cascade-failure")
def scenario_cascade_failure():
    """
    Scenario: Cascade failure — payments down → checkout fails → orders spike.
    Simulates a downstream dependency failure that cascades up the call chain.
    In production: look for correlated error spikes across multiple services.
    Owner: Platform team · Tier-1 · Runbook: circuit breaker, fallback to queue
    """
    with _failure_lock:
        _failure_modes["cascade_failure"] = True

    log.error("cascade_failure: ACTIVATED — payments degraded, cascade spreading to checkout and shipping")
    return jsonify(
        scenario="cascade-failure",
        status="ACTIVE",
        symptom="payments-service degraded → checkout error rate spiking → shipping timeouts",
        cascade_path=["payments-service → 503", "checkout → 502 (60%)", "shipping/track → 503 (55%)"],
        owner="Platform",
        runbook="1. Check payments-service health · 2. Enable circuit breaker · 3. Route to fallback queue",
        affected_services=["dt-port-demo", "payments-service", "shipping-service"]
    )

@app.route("/scenarios/cascade-failure/reset")
def scenario_cascade_failure_reset():
    with _failure_lock:
        _failure_modes["cascade_failure"] = False
    log.info("cascade_failure: reset — all services recovering")
    return jsonify(scenario="cascade-failure", status="reset")

@app.route("/scenarios/404-spike")
def scenario_404_spike():
    """
    Scenario: 404 spike — bad deploy introduced broken product URLs.
    Simulates a surge of 404s from a bad deploy that broke URL routing.
    In production: look for 404 rate spike, usually indicates bad deploy or CDN misconfiguration.
    Owner: Platform team · Tier-2 · Runbook: check recent deploy, CDN config
    """
    # Generate a burst of 404-inducing paths
    bad_skus = [f"SKU-{random.randint(900, 999):03d}" for _ in range(5)]
    errors = []
    for sku in bad_skus:
        log.error("404_spike: request for non-existent sku=%s", sku)
        errors.append({"sku": sku, "status": 404, "error": f"unknown sku {sku}"})

    return jsonify(
        scenario="404-spike",
        status="active",
        errors_generated=len(errors),
        symptom="surge of 404s on /products/<sku> — likely bad deploy broke URL routing",
        owner="Platform",
        runbook="Check recent deploy diff · Verify product SKU migration ran · Check CDN routing rules",
        sample_errors=errors
    )

@app.route("/scenarios/auth-failure")
def scenario_auth_failure():
    """
    Scenario: Auth service degradation — token validation latency spike.
    Simulates the auth service becoming slow and unreliable.
    In production: affects all authenticated endpoints, creates user-facing errors.
    Owner: Auth team · Tier-1 · Runbook: check JWT service, token cache
    """
    with _failure_lock:
        _failure_modes["auth_degraded"] = True

    log.error("auth_failure: ACTIVATED — token validation degraded, 45%% failure rate")
    return jsonify(
        scenario="auth-failure",
        status="ACTIVE",
        symptom="auth-service token validation failing at 45% rate with 1-3s latency",
        owner="Auth",
        runbook="1. Check JWT signing service · 2. Verify token cache hit rate · 3. Restart auth pods if needed",
        affected_endpoints=["/auth/verify", "/checkout", "/orders"]
    )

@app.route("/scenarios/auth-failure/reset")
def scenario_auth_failure_reset():
    with _failure_lock:
        _failure_modes["auth_degraded"] = False
    log.info("auth_failure: reset")
    return jsonify(scenario="auth-failure", status="reset")

@app.route("/scenarios/latency-spike")
def scenario_latency_spike():
    """
    Scenario: Latency spike — all endpoints suddenly slow.
    Simulates a noisy neighbour or resource contention causing global latency.
    In production: p99 spikes across all endpoints simultaneously.
    Owner: Platform team · Tier-1 · Runbook: check CPU/memory, noisy neighbour
    """
    with _failure_lock:
        _failure_modes["latency_spike"] = True

    log.error("latency_spike: ACTIVATED — adding 1.5-4s to all endpoint response times")
    return jsonify(
        scenario="latency-spike",
        status="ACTIVE",
        symptom="p99 latency spiking 1.5-4s across ALL endpoints — likely resource contention",
        owner="Platform",
        runbook="1. Check CPU/memory usage · 2. Look for noisy neighbour on host · 3. Check DB connection pool",
        affected_endpoints="ALL"
    )

@app.route("/scenarios/latency-spike/reset")
def scenario_latency_spike_reset():
    with _failure_lock:
        _failure_modes["latency_spike"] = False
    log.info("latency_spike: reset")
    return jsonify(scenario="latency-spike", status="reset")

@app.route("/scenarios/reset-all")
def scenario_reset_all():
    """Reset all failure modes at once."""
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
    """Check which failure modes are currently active."""
    return jsonify(
        failure_modes=_failure_modes,
        leak_bucket_size=len(_leak_bucket),
        active_scenarios=[k for k, v in _failure_modes.items() if v]
    )

# --- Legacy demo endpoints ----------------------------------------------------

@app.route("/healthz")
def healthz():
    active = [k for k, v in _failure_modes.items() if v]
    return jsonify(
        status="degraded" if active else "ok",
        active_failure_modes=active
    ), 200

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