#!/usr/bin/env bash
# loadgen.sh — realistic multi-service traffic generator for dt-port-demo
# Generates varied traffic across all services and failure scenarios
# so Datadog sees genuinely different incident types

BASE="http://localhost:8080"
SKUS=(SKU-001 SKU-002 SKU-003 SKU-004 SKU-005 SKU-006 SKU-007 SKU-008)
TRACKING_IDS=(TRK-001 TRK-002 TRK-003 TRK-004 TRK-005)

# ── helpers ──────────────────────────────────────────────────────────────────

get()  { curl -s -o /dev/null "$BASE$1" & }
post() { curl -s -o /dev/null -X POST "$BASE$1" "${@:2}" & }

shop() {
  # One simulated shopper: browse → add → checkout
  SKU=${SKUS[$RANDOM % ${#SKUS[@]}]}
  get "/products/$SKU"
  sleep 0.1
  post "/cart/add" -d "sku=$SKU&qty=1"
  sleep 0.1
  post "/checkout"
}

service_calls() {
  # Hit the microservice endpoints
  get "/payments/process"
  get "/auth/verify"
  get "/shipping/track/${TRACKING_IDS[$RANDOM % ${#TRACKING_IDS[@]}]}"
  get "/inventory/check"
}

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║         dt-port-demo  —  realistic loadgen               ║"
echo "║  Ctrl+C to stop · /scenarios/status to check modes      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

CYCLE=0

while true; do
  CYCLE=$((CYCLE + 1))
  echo "── Cycle $CYCLE ──────────────────────────────────────────────"

  # ── Phase 1: Baseline traffic (always running) ──────────────────
  echo "  Phase 1: baseline traffic (10 shoppers + service calls)"
  for i in $(seq 1 10); do shop; done
  for i in $(seq 1 5); do service_calls; done
  get "/shipping/rates"
  get "/orders"
  wait
  sleep 2

  # ── Phase 2: Rotate through failure scenarios ───────────────────
  # Pick a random scenario each cycle so incidents vary
  SCENARIO=$((CYCLE % 6))

  case $SCENARIO in
    0)
      echo "  Phase 2: SCENARIO — memory leak"
      for i in $(seq 1 8); do get "/scenarios/memory-leak"; done
      for i in $(seq 1 15); do shop; done
      wait; sleep 3
      get "/scenarios/memory-leak/reset"
      ;;
    1)
      echo "  Phase 2: SCENARIO — slow DB query"
      for i in $(seq 1 5); do get "/scenarios/slow-db" & done
      # While DB is slow, hammer checkout to spike latency
      for i in $(seq 1 20); do post "/checkout"; done
      wait; sleep 3
      get "/scenarios/slow-db/reset"
      ;;
    2)
      echo "  Phase 2: SCENARIO — cascade failure"
      get "/scenarios/cascade-failure"
      sleep 1
      # Lots of checkout + payments calls during cascade
      for i in $(seq 1 30); do
        post "/checkout" &
        get "/payments/process" &
        get "/shipping/track/TRK-001" &
      done
      wait; sleep 5
      get "/scenarios/cascade-failure/reset"
      ;;
    3)
      echo "  Phase 2: SCENARIO — 404 spike"
      # Hit non-existent SKUs
      for i in $(seq 1 20); do
        get "/products/SKU-$((RANDOM % 900 + 100))" &
      done
      for i in $(seq 1 5); do get "/scenarios/404-spike" & done
      wait; sleep 2
      ;;
    4)
      echo "  Phase 2: SCENARIO — auth service degradation"
      get "/scenarios/auth-failure"
      sleep 1
      for i in $(seq 1 25); do
        get "/auth/verify" &
        post "/checkout" &
      done
      wait; sleep 4
      get "/scenarios/auth-failure/reset"
      ;;
    5)
      echo "  Phase 2: SCENARIO — latency spike"
      get "/scenarios/latency-spike"
      sleep 1
      for i in $(seq 1 15); do
        get "/products/${SKUS[$RANDOM % ${#SKUS[@]}]}" &
        post "/checkout" &
        get "/payments/process" &
      done
      wait; sleep 5
      get "/scenarios/latency-spike/reset"
      ;;
  esac

  wait

  # ── Phase 3: Flash sale burst ────────────────────────────────────
  echo "  Phase 3: flash sale (30 parallel shoppers)"
  for i in $(seq 1 30); do shop; done
  wait
  sleep 1

  # ── Phase 4: Service health check ───────────────────────────────
  echo "  Phase 4: service health sweep"
  get "/healthz"
  get "/scenarios/status"
  get "/inventory/check"
  get "/auth/verify"
  get "/payments/process"
  wait

  # ── Phase 5: Cool-down ───────────────────────────────────────────
  echo "  Phase 5: cool-down (15s)"
  for i in $(seq 1 5); do shop; done
  wait
  sleep 15

  echo "  ✓ Cycle $CYCLE complete — check Datadog APM + Port incidents"
  echo ""
done