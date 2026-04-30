#!/usr/bin/env bash
# demo_remote_runtime.sh — Multi-host simulation demo for aMaze
#
# Starts the platform, agents, and MCP server as three separate compose
# projects (no shared Docker network). Simulates the "agents on a different
# host" deployment using host.docker.internal for cross-project routing.
#
# Prerequisites:
#   - Docker compose v2 (docker compose, not docker-compose)
#   - OPENAI_API_KEY set in the environment (or .env at repo root)
#   - TAVILY_API_KEY set (optional; web_search falls back gracefully)
#
# Usage:
#   cd /path/to/aMaze
#   OPENAI_API_KEY=sk-... bash scripts/demo_remote_runtime.sh

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM_COMPOSE="$REPO_ROOT/docker/docker-compose.yml"
AGENTS_COMPOSE="$REPO_ROOT/docker/compose-agent-host.yml"
MCP_COMPOSE="$REPO_ROOT/docker/compose-mcp-host.yml"

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { printf "${GREEN}[demo]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[warn]${NC} %s\n" "$*"; }
fail() { printf "${RED}[fail]${NC} %s\n" "$*"; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
log "Checking Docker compose v2..."
docker compose version >/dev/null 2>&1 || fail "docker compose v2 not found"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    warn "OPENAI_API_KEY is not set — LLM calls will be rejected by the proxy"
fi

# ── Step 1: Start the control plane ──────────────────────────────────────────
log "Step 1: Starting aMaze platform (platform + redis + jaeger)..."
docker compose -f "$PLATFORM_COMPOSE" up -d --build

# Wait for orchestrator to be ready
log "Waiting for orchestrator on :8001..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8001/health >/dev/null 2>&1; then
        log "Orchestrator is up."
        break
    fi
    if [[ $i -eq 30 ]]; then
        fail "Orchestrator did not become healthy after 30s"
    fi
    sleep 1
done

# Wait for proxy to be ready
log "Waiting for proxy on :8080..."
for i in $(seq 1 30); do
    if curl -sf --proxy http://localhost:8080 http://localhost:8001/health >/dev/null 2>&1; then
        log "Proxy is up."
        break
    fi
    if [[ $i -eq 30 ]]; then
        warn "Proxy health check inconclusive — continuing anyway"
        break
    fi
    sleep 1
done

# ── Step 2: Start MCP server (separate compose project) ──────────────────────
log "Step 2: Starting MCP server (separate compose project: amaze-mcp)..."
docker compose -f "$MCP_COMPOSE" up -d --build

# Wait for MCP server to be ready
log "Waiting for MCP server on :8000..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/ >/dev/null 2>&1; then
        log "MCP server is up."
        break
    fi
    if [[ $i -eq 30 ]]; then
        warn "MCP server health check inconclusive — continuing anyway"
        break
    fi
    sleep 1
done

# ── Step 3: Start agents (separate compose project) ───────────────────────────
log "Step 3: Starting agents (separate compose project: amaze-agents)..."
docker compose -f "$AGENTS_COMPOSE" up -d --build

# Wait for agent-sdk chat endpoint to be ready
log "Waiting for agent-sdk /chat on :8090..."
for i in $(seq 1 60); do
    if curl -sf http://localhost:8090/health >/dev/null 2>&1 || \
       curl -sf -o /dev/null -w "%{http_code}" http://localhost:8090/ 2>/dev/null | grep -qE '^[0-9]'; then
        log "agent-sdk is up."
        break
    fi
    if [[ $i -eq 60 ]]; then
        warn "agent-sdk readiness check inconclusive — continuing anyway"
        break
    fi
    sleep 1
done

# ── Step 4: Check Redis for registered endpoints ──────────────────────────────
log "Step 4: Checking Redis for registered agent endpoints..."
REDIS_CTR="$(docker ps --filter name=amaze-redis --format '{{.Names}}' | head -1)"
if [[ -z "$REDIS_CTR" ]]; then
    warn "amaze-redis container not found — skipping Redis check"
else
    for agent in agent-sdk agent-sdk1 agent-sdk2; do
        endpoint="$(docker exec "$REDIS_CTR" redis-cli GET "agent:${agent}:endpoint" 2>/dev/null || true)"
        if [[ -n "$endpoint" ]]; then
            log "  agent:${agent}:endpoint = $endpoint"
        else
            warn "  agent:${agent}:endpoint NOT registered yet"
        fi
    done

    mcp_val="$(docker exec "$REDIS_CTR" redis-cli GET "mcp:demo-mcp" 2>/dev/null || true)"
    if [[ -n "$mcp_val" ]]; then
        log "  mcp:demo-mcp = $mcp_val"
    else
        warn "  mcp:demo-mcp NOT registered yet"
    fi
fi

# ── Step 5: Run a test chat request ───────────────────────────────────────────
log "Step 5: Sending test chat request to agent-sdk..."
RESPONSE="$(curl -sf -X POST http://localhost:8090/chat \
    -H "Content-Type: application/json" \
    -d '{"message": "search for current bitcoin price"}' \
    --max-time 60 || true)"

if [[ -n "$RESPONSE" ]]; then
    log "Chat response received:"
    printf '%s\n' "$RESPONSE" | head -20
else
    warn "No response from /chat endpoint (LLM key missing or timeout)"
fi

# ── Step 6: Show audit log from Redis ────────────────────────────────────────
log "Step 6: Audit log tail (audit:global, last 5 entries)..."
if [[ -n "$REDIS_CTR" ]]; then
    docker exec "$REDIS_CTR" redis-cli XREVRANGE audit:global + - COUNT 5 2>/dev/null \
        | grep -E '^[0-9]|agent_id|kind|target|denied|denial_reason' \
        || warn "No audit entries found yet"
else
    warn "Skipping audit log — redis container not found"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
log ""
log "Demo runtime is up. Useful endpoints:"
log "  Agent chat:  http://localhost:8090/chat  (POST)"
log "  Orchestrator: http://localhost:8001"
log "  Jaeger UI:   http://localhost:16686"
log ""
log "To stop all projects:"
log "  docker compose -f docker/docker-compose.yml down"
log "  docker compose -f docker/compose-agent-host.yml down"
log "  docker compose -f docker/compose-mcp-host.yml down"
