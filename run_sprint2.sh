#!/usr/bin/env bash
# run_sprint2.sh — Sprint 2 demo: A2A allow/deny enforcement through Envoy + Policy Processor
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/home/ubuntu/venv/bin/python"
ENVOY_IMAGE="envoyproxy/envoy:v1.33-latest"

cleanup() {
  echo ""
  echo "[sprint2] shutting down..."
  kill "$PP_PID" 2>/dev/null || true
  docker stop amaze-envoy 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

# ── kill stale processes on agent/envoy/policy-processor ports ──────────────
for port in 9001 9002 10000 9901 50051; do
  pids=$(lsof -ti tcp:$port 2>/dev/null || true)
  [ -n "$pids" ] && echo "[sprint2] clearing port $port (pid $pids)" && kill -9 $pids 2>/dev/null || true
done
docker stop amaze-envoy 2>/dev/null || true
sleep 0.5

# ── 1. Start Policy Processor ────────────────────────────────────────────────
echo "[sprint2] starting Policy Processor on :50051..."
cd "$REPO"
PYTHONPATH="$REPO:$REPO/policy_processor/proto" \
  $PYTHON policy_processor/server.py &
PP_PID=$!
sleep 1

# ── 2. Start Envoy ───────────────────────────────────────────────────────────
echo "[sprint2] starting Envoy on :10000 (Docker --network=host)..."
docker run --rm --network=host \
  -v "$REPO/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro" \
  --name amaze-envoy \
  "$ENVOY_IMAGE" \
  envoy -c /etc/envoy/envoy.yaml --log-level warning &
sleep 3

# ── 3. Run Sprint 2 system tests ─────────────────────────────────────────────
echo "[sprint2] running system tests..."
echo "──────────────────────────────────────────"
PYTHONPATH="$REPO:$REPO/policy_processor/proto" \
  $PYTHON tests/system_test_sprint2.py || true
echo "──────────────────────────────────────────"

cleanup
