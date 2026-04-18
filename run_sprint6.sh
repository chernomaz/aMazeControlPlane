#!/usr/bin/env bash
set -euo pipefail
export PATH=$PATH:/usr/local/go/bin
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "=== Sprint 6: Per-agent Token Limits ==="

# ── Kill existing services ────────────────────────────────────────────────────
for port in 50051 10000 9901 9003; do
  fuser -k -TERM "$port/tcp" 2>/dev/null || true
done
docker rm -f amaze-envoy 2>/dev/null || true
sleep 1

# ── Build Go processor ────────────────────────────────────────────────────────
echo "[1/4] Building Go processor..."
cd "$REPO/go_processor"
go mod tidy
go build -o "$REPO/go_processor/go-policy-processor" ./cmd/server/
echo "      Build OK"
cd "$REPO"

# ── Start Go processor ────────────────────────────────────────────────────────
echo "[2/4] Starting Go policy processor on :50051..."
POLICY_PATH="$REPO/policy_processor/policies/agents.yaml" \
  "$REPO/go_processor/go-policy-processor" &
GO_PP_PID=$!
sleep 1

# ── Start Envoy ───────────────────────────────────────────────────────────────
echo "[3/4] Starting Envoy on :10000..."
docker run --rm --network=host \
  -v "$REPO/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro" \
  --name amaze-envoy \
  envoyproxy/envoy:v1.32.0 \
  envoy -c /etc/envoy/envoy.yaml --log-level warning &
sleep 3

# ── Start MCP server (needed for Sprint 3/4 backward-compat) ─────────────────
echo "[4/4] Starting MCP server on :9003..."
PYTHONPATH="$REPO:$REPO/policy_processor/proto" \
  /home/ubuntu/venv/bin/python examples/mcp_server/server.py &
MCP_PID=$!
sleep 2

echo ""
echo "Stack running:"
echo "  Go processor PID : $GO_PP_PID"
echo "  MCP server PID   : $MCP_PID"
echo "  Envoy             : docker (amaze-envoy)"
echo ""
echo "Run tests:"
echo "  /home/ubuntu/venv/bin/python tests/system_test_sprint6.py"
