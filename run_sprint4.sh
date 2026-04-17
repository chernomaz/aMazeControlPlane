#!/usr/bin/env bash
# run_sprint4.sh — start the full Sprint 4 stack
#   Policy Processor  :50051
#   Envoy             :10000  (admin :9901)
#   MCP Server        :9003

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
VENV=/home/ubuntu/venv
PY="$VENV/bin/python"

for port in 9003 10000 50051; do
    fuser -k -TERM "$port/tcp" 2>/dev/null || true
done
sleep 1

echo "=== Starting Policy Processor (:50051) ==="
PYTHONPATH="$REPO:$REPO/policy_processor/proto" \
    $PY "$REPO/policy_processor/server.py" &
PP_PID=$!
sleep 1

echo "=== Starting Envoy (:10000) ==="
docker run --rm --network=host \
    -v "$REPO/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro" \
    --name amaze-envoy \
    envoyproxy/envoy:v1.33-latest \
    envoy -c /etc/envoy/envoy.yaml --log-level warning &
ENVOY_PID=$!
sleep 3

echo "=== Starting MCP Server (:9003) ==="
PYTHONPATH="$REPO" \
    $PY "$REPO/examples/mcp_server_sprint3/main.py" &
MCP_PID=$!
sleep 2

echo ""
echo "Stack running:"
echo "  Policy Processor PID=$PP_PID   (:50051)"
echo "  Envoy            PID=$ENVOY_PID  (:10000)"
echo "  MCP Server       PID=$MCP_PID   (:9003)"
echo ""
echo "Run tests:  $VENV/bin/python tests/system_test_sprint4.py"
echo "Press Ctrl-C to stop."

wait
