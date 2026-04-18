#!/usr/bin/env bash
# run_demo_integration.sh — Integration demo: 4-step LangChain agent through Envoy
#
#   Services started:
#     Policy Processor   :50051
#     Envoy              :10000  (admin :9901)
#     mcp-server-sprint3 :9003   (Host: mcp-server)  — echo tool
#     mcp-server (main)  :9004   (Host: search-mcp)  — web_search tool
#     agent-b            :9002   (Host: agent-b)      — A2A target
#
#   Demo agent:  examples/agents/demo_agent/main.py
#     Step 1 — LLM call       (ChatOpenAI gpt-4o-mini)
#     Step 2 — web_search     via search-mcp through Envoy
#     Step 3 — echo           via mcp-server through Envoy
#     Step 4 — A2A tasks/send to agent-b through Envoy

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
VENV=/home/ubuntu/venv
PY="$VENV/bin/python"

# ── Cleanup ──────────────────────────────────────────────────────────────────
echo "=== Stopping any existing services ==="
for port in 9002 9003 9004 10000 50051; do
    fuser -k -TERM "$port/tcp" 2>/dev/null || true
done
docker rm -f amaze-envoy 2>/dev/null || true
sleep 1

PIDS=()

cleanup() {
    echo ""
    echo "=== Stopping all services ==="
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    docker rm -f amaze-envoy 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Policy Processor ─────────────────────────────────────────────────────────
echo "=== Starting Policy Processor (:50051) ==="
PYTHONPATH="$REPO:$REPO/policy_processor/proto" \
    $PY "$REPO/policy_processor/server.py" &
PIDS+=($!)
sleep 1

# ── Envoy ─────────────────────────────────────────────────────────────────────
echo "=== Starting Envoy (:10000) ==="
docker run --rm --network=host \
    -v "$REPO/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro" \
    --name amaze-envoy \
    envoyproxy/envoy:v1.33-latest \
    envoy -c /etc/envoy/envoy.yaml --log-level warning &
PIDS+=($!)
sleep 3

# ── MCP server (sprint3) — echo tool — :9003 ─────────────────────────────────
echo "=== Starting mcp-server (echo) on :9003 ==="
PYTHONPATH="$REPO" \
    $PY "$REPO/examples/mcp_server_sprint3/main.py" &
PIDS+=($!)
sleep 2

# ── MCP server (main) — web_search tool — :9004 ──────────────────────────────
echo "=== Starting search-mcp (web_search) on :9004 ==="
PORT=9004 PYTHONPATH="$REPO" \
    $PY "$REPO/examples/mcp_server/server.py" &
PIDS+=($!)
sleep 2

# ── Agent B ───────────────────────────────────────────────────────────────────
echo "=== Starting agent-b on :9002 ==="
PYTHONPATH="$REPO/examples/agents" \
    $PY "$REPO/examples/agents/agent_b/main.py" &
PIDS+=($!)
sleep 2

# ── Health checks ─────────────────────────────────────────────────────────────
echo ""
echo "=== Waiting for services to be ready ==="

wait_for_port() {
    local label="$1" port="$2" retries=15
    for i in $(seq 1 $retries); do
        if curl -sf "http://localhost:$port" -o /dev/null 2>/dev/null || \
           curl -sf "http://localhost:$port/mcp" -o /dev/null 2>/dev/null || \
           nc -z localhost "$port" 2>/dev/null; then
            echo "  ✓ $label (:$port) ready"
            return 0
        fi
        sleep 1
    done
    echo "  ✗ $label (:$port) did not become ready in ${retries}s" >&2
    return 1
}

wait_for_port "Envoy"              10000
wait_for_port "mcp-server (echo)"  9003
wait_for_port "search-mcp"         9004
wait_for_port "agent-b"            9002

# ── Run demo ──────────────────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo "  All services ready.  Running integration demo agent..."
echo "================================================================"
echo ""

PYTHONPATH="$REPO/examples/agents" \
    $PY "$REPO/examples/agents/demo_agent/main.py"

echo ""
echo "================================================================"
echo "  Demo finished.  Press Ctrl-C to stop all services."
echo "================================================================"

wait
