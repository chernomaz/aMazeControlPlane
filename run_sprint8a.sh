#!/usr/bin/env bash
# Sprint 8 — Phase 8A: NEMO Container + Orchestrator.
#
# Boots the full enforcement stack in Docker Compose, waits for health,
# then runs the Phase 8A system tests against the real stack (no mocks).
#
#   Usage:
#     ./run_sprint8a.sh            # build + up + run tests
#     ./run_sprint8a.sh --no-test  # just boot the stack (for the demo UI)
#     ./run_sprint8a.sh --down     # tear the stack down
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f $REPO/docker/docker-compose.nemo.yml"

cmd=${1:-run}

case "$cmd" in
  --down|down)
    echo "[sprint8a] tearing stack down"
    $COMPOSE down -v --remove-orphans
    exit 0
    ;;
esac

echo "=== Sprint 8 — Phase 8A: NEMO + Orchestrator ==="

# Clean any previous run.
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true

echo "[1/3] Building images"
$COMPOSE build

echo "[2/3] Starting stack"
$COMPOSE up -d

echo "      Waiting for orchestrator :7000"
for i in {1..60}; do
  if curl -sf http://localhost:7000/agents >/dev/null 2>&1; then
    echo "      orchestrator OK"
    break
  fi
  sleep 1
  if [ "$i" -eq 60 ]; then
    echo "[sprint8a] orchestrator never came up" >&2
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
    echo "[sprint8a] policy-processor never came up" >&2
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
    echo "[sprint8a] agents did not register (got $count)" >&2
    $COMPOSE logs agent-a agent-b orchestrator | tail -60 >&2
    exit 1
  fi
done

if [ "$cmd" = "--no-test" ] || [ "$cmd" = "no-test" ]; then
  echo ""
  echo "Stack up. Chat UI: http://localhost:7000/"
  echo "Tear down with: $0 --down"
  exit 0
fi

echo "[3/3] Running system tests"
set +e
/home/ubuntu/venv/bin/python "$REPO/tests/system_test_sprint8a.py"
rc=$?
set -e

if [ $rc -ne 0 ]; then
  echo ""
  echo "[sprint8a] tests failed — compose logs tail:"
  $COMPOSE logs --tail=30
  exit $rc
fi

echo ""
echo "=== Sprint 8 Phase 8A — all tests green ==="
echo "Chat UI still running at http://localhost:7000/  (run '$0 --down' to stop)"
