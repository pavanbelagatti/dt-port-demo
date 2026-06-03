#!/bin/bash
# Simulates real shopper behavior: browse → add to cart → maybe checkout.
# Includes occasional shipping-rate lookups and a few error endpoints
# to keep Dynatrace dashboards interesting. Runs ~6-8 min.

URL="http://localhost:8080"
SKUS=("SKU-001" "SKU-002" "SKU-003" "SKU-004" "SKU-005" "SKU-006" "SKU-007" "SKU-008")

# Each "shopper" gets its own cookie jar so the cart works
shopper() {
  local id=$1
  local jar="/tmp/shopdemo-jar-$id.txt"
  rm -f "$jar"

  # Browse home
  curl -s -c "$jar" -b "$jar" "$URL/" > /dev/null

  # Look at 2-4 products
  local views=$((RANDOM % 3 + 2))
  for ((i=0; i<views; i++)); do
    local sku=${SKUS[$((RANDOM % ${#SKUS[@]}))]}
    curl -s -c "$jar" -b "$jar" "$URL/products/$sku" > /dev/null
    sleep 0.$((RANDOM % 9))
  done

  # 80% add at least one thing to cart
  if [ $((RANDOM % 10)) -lt 8 ]; then
    local sku=${SKUS[$((RANDOM % ${#SKUS[@]}))]}
    curl -s -c "$jar" -b "$jar" -X POST -d "sku=$sku&qty=1" "$URL/cart/add" > /dev/null
    sleep 0.$((RANDOM % 5))

    # 60% look at the cart
    if [ $((RANDOM % 10)) -lt 6 ]; then
      curl -s -c "$jar" -b "$jar" "$URL/cart" > /dev/null

      # 50% try shipping rates
      if [ $((RANDOM % 10)) -lt 5 ]; then
        curl -s -c "$jar" -b "$jar" "$URL/shipping/rates" > /dev/null
      fi

      # 70% actually try checkout (some will fail at ~5% rate intentionally)
      if [ $((RANDOM % 10)) -lt 7 ]; then
        curl -s -c "$jar" -b "$jar" -X POST "$URL/checkout" > /dev/null
      fi
    fi
  fi

  rm -f "$jar"
}

echo "=== Phase 1: Light steady traffic (15 shoppers, paced) ==="
for i in {1..15}; do
  shopper $i &
  sleep 1
done
wait

echo ""
echo "=== Phase 2: Quiet period (10s, almost no traffic) ==="
curl -s "$URL/healthz" > /dev/null
sleep 10

echo ""
echo "=== Phase 3: Flash sale (40 shoppers in parallel) ==="
for i in {100..140}; do
  shopper $i &
done
wait
echo "  flash sale done"

echo ""
echo "=== Phase 4: Sustained chaos (60s) ==="
end=$((SECONDS+60))
i=200
while [ $SECONDS -lt $end ]; do
  shopper $i &
  i=$((i+1))
  # occasional non-shopper traffic so it's not all checkout
  if [ $((RANDOM % 5)) -eq 0 ]; then
    curl -s "$URL/slow" > /dev/null &
  fi
  if [ $((RANDOM % 25)) -eq 0 ]; then
    curl -s "$URL/error" > /dev/null &
  fi
  sleep 0.$((RANDOM % 9))
done
wait

echo ""
echo "=== Phase 5: Cool-down (light browsing, 30s) ==="
for i in {300..305}; do
  shopper $i &
  sleep 5
done
wait

echo ""
echo "=== Done. Wait ~90s then check Dynatrace. ==="