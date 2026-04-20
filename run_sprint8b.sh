#!/usr/bin/env bash
# Sprint 8 — Phase 8B: substrate extensions (MCP / LiteLLM / bearer / cross-org).
#
# Boots the Phase 8A stack plus the Phase 8B additions, waits for health,
# runs the 8A regression suite and the 8B system tests in sequence against
# the real stack (no mocks).
#
#   Usage:
#     ./run_sprint8b.sh            # build + up + run 8A + 8B tests
#     ./run_sprint8b.sh --no-test  # just boot the stack
#     ./run_sprint8b.sh --down     # tear the stack down
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f $REPO/docker/docker-compose.yml"

cmd=${1:-run}

case "$cmd" in
  --down|down)
    echo "[sprint8b] tearing stack down"
    $COMPOSE down -v --remove-orphans
    exit 0
    ;;
esac

echo "=== Sprint 8 — Phase 8B: substrate extensions ==="

$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true

echo "[1/4] Building images"
$COMPOSE build

echo "[2/4] Starting stack"
$COMPOSE up -d

echo "      Waiting for orchestrator :7000"
for i in {1..60}; do
  if curl -sf http://localhost:7000/agents >/dev/null 2>&1; then
    echo "      orchestrator OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 60 ]; then
    echo "[sprint8b] orchestrator never came up" >&2
    $COMPOSE logs orchestrator | tail -40 >&2
    exit 1
  fi
done

echo "      Waiting for policy-processor config API :8082"
for i in {1..30}; do
  if curl -sf http://localhost:8082/config/agents >/dev/null 2>&1; then
    echo "      policy-processor OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    echo "[sprint8b] policy-processor never came up" >&2
    $COMPOSE logs policy-processor | tail -40 >&2
    exit 1
  fi
done

echo "      Waiting for agents to register"
for i in {1..30}; do
  count=$(curl -sf http://localhost:7000/agents | /home/ubuntu/venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("agents") or []))' 2>/dev/null || echo 0)
  if [ "$count" = "2" ]; then
    echo "      agents registered ($count)"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    echo "[sprint8b] agents did not register (got $count)" >&2
    $COMPOSE logs agent-a agent-b orchestrator | tail -60 >&2
    exit 1
  fi
done

echo "      Waiting for MCP server to register"
for i in {1..60}; do
  count=$(curl -sf http://localhost:7000/mcp | /home/ubuntu/venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("mcp_servers") or []))' 2>/dev/null || echo 0)
  if [ "$count" = "1" ]; then
    echo "      mcp registered ($count)"
    break
  fi
  sleep 1
  if [ "$i" -eq 60 ]; then
    echo "[sprint8b] mcp-5-tools did not register (got $count)" >&2
    $COMPOSE logs mcp-5-tools orchestrator | tail -80 >&2
    exit 1
  fi
done

echo "      Waiting for a2a-proxy sidecar to be reachable"
# agent-a has HTTP_PROXY=envoy set, and urllib.request honours that by
# default. Clear the proxy env for these probes so we're checking the
# sidecars directly (not testing Envoy routing to them).
for i in {1..30}; do
  if $COMPOSE exec -T -e HTTP_PROXY= -e HTTPS_PROXY= -e http_proxy= -e https_proxy= agent-a \
      python -c "import urllib.request; urllib.request.urlopen('http://a2a-proxy:8082/healthz', timeout=2).read()" >/dev/null 2>&1; then
    echo "      a2a-proxy OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    echo "[sprint8b] a2a-proxy never became reachable" >&2
    $COMPOSE logs a2a-proxy | tail -40 >&2
    exit 1
  fi
done

echo "      Waiting for partner-agent to serve TLS (dialed via a2a-proxy)"
# Probe https://partner-agent.example.com/ through the a2a-proxy sidecar
# so the TLS handshake + the partner's FastAPI startup are both covered.
# Any HTTPResponse (including 404) proves the partner is alive.
for i in {1..30}; do
  if $COMPOSE exec -T -e HTTP_PROXY= -e HTTPS_PROXY= -e http_proxy= -e https_proxy= agent-a \
      python -c "
import sys, urllib.request, urllib.error
req = urllib.request.Request('http://a2a-proxy:8082/healthz', headers={'Host': 'partner-agent.example.com'}, method='GET')
try:
    urllib.request.urlopen(req, timeout=2).read()
    sys.exit(0)
except urllib.error.HTTPError:
    sys.exit(0)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; then
    echo "      partner-agent OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    echo "[sprint8b] partner-agent never became reachable" >&2
    $COMPOSE logs partner-agent a2a-proxy | tail -60 >&2
    exit 1
  fi
done

echo "      Waiting for LiteLLM sidecar to be reachable"
# LiteLLM isn't exposed to the host, so probe from a container that has
# python (agent-a). urllib.request avoids any extra package installs.
for i in {1..90}; do
  if $COMPOSE exec -T agent-a python -c "import urllib.request, sys; urllib.request.urlopen('http://litellm:4000/health/liveliness', timeout=2).read()" >/dev/null 2>&1; then
    echo "      litellm OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 90 ]; then
    echo "[sprint8b] litellm never became reachable" >&2
    $COMPOSE logs litellm | tail -60 >&2
    exit 1
  fi
done

if [ "$cmd" = "--no-test" ] || [ "$cmd" = "no-test" ]; then
  echo ""
  echo "Stack up. Chat UI: http://localhost:7000/"
  echo "Tear down with: $0 --down"
  exit 0
fi

echo "[3/6] Running Phase 8A regression"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8a.py"
rc_a=$?
set -e

echo "[4/6] Running Phase 8B Slice 1/2 system tests"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b.py"
rc_b=$?
set -e

echo "[5/6] Running Phase 8B Slice 3 system tests (LiteLLM)"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b_slice3.py"
rc_s3=$?
set -e

echo "[6/7] Running Phase 8B Slice 4 system tests (A2A bearer)"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b_slice4.py"
rc_s4=$?
set -e

echo "[7/7] Running Phase 8B Slice 5 system tests (cross-org A2A)"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b_slice5.py"
rc_s5=$?
set -e

if [ $rc_a -ne 0 ] || [ $rc_b -ne 0 ] || [ $rc_s3 -ne 0 ] || [ $rc_s4 -ne 0 ] || [ $rc_s5 -ne 0 ]; then
  echo ""
  echo "[sprint8b] tests failed (8A=$rc_a, 8B=$rc_b, 8B-slice3=$rc_s3, 8B-slice4=$rc_s4, 8B-slice5=$rc_s5) — compose logs tail:"
  $COMPOSE logs --tail=40
  exit 1
fi

echo ""
echo "=== Sprint 8 Phase 8B — all tests green ==="
echo "Chat UI still running at http://localhost:7000/  (run '$0 --down' to stop)"
