#!/usr/bin/env bash
# Sprint 9 — Agent SDK.
#
# Boots the full Sprint-8 stack plus the three sdk-agent-* containers,
# runs the Phase 8A + 8B regression suites, then the Sprint 9 tests.
#
#   Usage:
#     ./run_sprint9.sh            # build + up + run 8A + all 8B + 9 tests
#     ./run_sprint9.sh --no-test  # just boot the stack
#     ./run_sprint9.sh --down     # tear the stack down
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f $REPO/docker/docker-compose.yml"

cmd=${1:-run}

case "$cmd" in
  --down|down)
    echo "[sprint9] tearing stack down"
    $COMPOSE down -v --remove-orphans
    exit 0
    ;;
esac

echo "=== Sprint 9 — Agent SDK ==="

$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true

echo "[1/5] Building images"
$COMPOSE build

echo "[2/5] Starting stack"
$COMPOSE up -d

echo "      Waiting for orchestrator :7000"
for i in {1..60}; do
  if curl -sf http://localhost:7000/agents >/dev/null 2>&1; then
    echo "      orchestrator OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 60 ]; then
    echo "[sprint9] orchestrator never came up" >&2
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
    echo "[sprint9] policy-processor never came up" >&2
    $COMPOSE logs policy-processor | tail -40 >&2
    exit 1
  fi
done

echo "      Waiting for 5 agents to register (agent-a, agent-b, sdk-agent-a/b/llm)"
for i in {1..60}; do
  count=$(curl -sf http://localhost:7000/agents | /home/ubuntu/venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("agents") or []))' 2>/dev/null || echo 0)
  if [ "$count" = "5" ]; then
    echo "      agents registered ($count)"
    break
  fi
  sleep 1
  if [ "$i" -eq 60 ]; then
    echo "[sprint9] agents did not all register (got $count)" >&2
    $COMPOSE logs agent-a agent-b sdk-agent-a sdk-agent-b sdk-agent-llm orchestrator | tail -100 >&2
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
    echo "[sprint9] mcp-5-tools did not register (got $count)" >&2
    $COMPOSE logs mcp-5-tools orchestrator | tail -80 >&2
    exit 1
  fi
done

echo "      Waiting for sidecars (a2a-proxy, partner-agent, litellm)"
for i in {1..30}; do
  if $COMPOSE exec -T -e HTTP_PROXY= -e HTTPS_PROXY= agent-a \
      python -c "import urllib.request; urllib.request.urlopen('http://a2a-proxy:8082/healthz', timeout=2).read()" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    echo "[sprint9] a2a-proxy not reachable" >&2
    exit 1
  fi
done
for i in {1..30}; do
  if $COMPOSE exec -T -e HTTP_PROXY= -e HTTPS_PROXY= agent-a \
      python -c "
import urllib.request, urllib.error, sys
req = urllib.request.Request('http://a2a-proxy:8082/healthz', headers={'Host': 'partner-agent.example.com'}, method='GET')
try: urllib.request.urlopen(req, timeout=2).read(); sys.exit(0)
except urllib.error.HTTPError: sys.exit(0)
except Exception: sys.exit(1)
" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    echo "[sprint9] partner-agent not reachable" >&2
    exit 1
  fi
done
for i in {1..90}; do
  if $COMPOSE exec -T agent-a python -c "import urllib.request; urllib.request.urlopen('http://litellm:4000/health/liveliness', timeout=2).read()" >/dev/null 2>&1; then
    echo "      sidecars OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 90 ]; then
    echo "[sprint9] litellm not reachable" >&2
    exit 1
  fi
done

if [ "$cmd" = "--no-test" ] || [ "$cmd" = "no-test" ]; then
  echo ""
  echo "Stack up. Chat UI: http://localhost:7000/"
  echo "Tear down with: $0 --down"
  exit 0
fi

echo "[3/5] Running Phase 8A regression"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8a.py"
rc_a=$?
set -e

echo "[4/5] Running Phase 8B (Slice 1/2 + 3 + 4 + 5)"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b.py";           rc_b=$?
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b_slice3.py";    rc_s3=$?
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b_slice4.py";    rc_s4=$?
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8b_slice5.py";    rc_s5=$?
set -e

echo "[5/5] Running Sprint 9 system tests"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint9.py"
rc_9=$?
set -e

failed=0
for rc in $rc_a $rc_b $rc_s3 $rc_s4 $rc_s5 $rc_9; do
  if [ "$rc" -ne 0 ]; then failed=1; fi
done

if [ $failed -ne 0 ]; then
  echo ""
  echo "[sprint9] tests failed (8A=$rc_a, 8B=$rc_b, S3=$rc_s3, S4=$rc_s4, S5=$rc_s5, 9=$rc_9) — compose logs tail:"
  $COMPOSE logs --tail=40
  exit 1
fi

echo ""
echo "=== Sprint 9 — all tests green ==="
echo "Chat UI still running at http://localhost:7000/  (run '$0 --down' to stop)"
