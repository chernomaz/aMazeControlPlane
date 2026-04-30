"""
Sprint S3 system tests — ST-S3.1 through ST-S3.7.

Validates the Redis-backed endpoint registration and Router addon:
  1. Correct HTTP status + body
  2. Audit record written to Redis Stream with correct fields
  3. Redis counters updated where applicable

Prerequisites
─────────────
  docker compose -f tests/compose.test.yml up --build -d
  pip install -r tests/requirements.txt
  pytest tests/test_s3.py -v

Environment overrides (all optional):
  AMAZE_PROXY        http://localhost:18080
  AMAZE_ORCHESTRATOR http://localhost:18001
  REDIS_HOST         localhost
  REDIS_PORT         16379

What is being tested
─────────────────────
S3 adds the Router addon (last in the chain). It reads Redis keys:
  - `agent:{id}:endpoint` for A2A routing
  - `mcp:{name}` for MCP routing (already existed)
and rewrites flow.request.host + port before mitmproxy connects.

The key new invariant: A2A calls to an agent that has NOT registered its
endpoint are denied 503 `agent-not-registered` (fail-closed). Tests verify
both the happy path (registered → 200) and the failure path (not registered
→ 503).

NOT PARALLEL-SAFE
──────────────────
Each test registers a fresh session. The per-agent audit stream is cleared
before each test. Running two processes against the same platform will
cross-contaminate audit records. Run pytest sequentially (default).
"""
from __future__ import annotations

import json
import os
import time

import httpx
import pytest
import redis

# ── connection settings ────────────────────────────────────────────────────

PROXY = os.environ.get("AMAZE_PROXY", "http://localhost:18080")
ORCH  = os.environ.get("AMAZE_ORCHESTRATOR", "http://localhost:18001")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "16379"))

# Mock service names (Docker DNS in the test compose project)
MCP_HOST   = "mock-mcp"
A2A_CALLEE = "test-s3-callee"   # logical agent name; Router resolves → mock-agent:8000
A2A_CALLER = "test-s3-caller"

LLM_URL = "http://api.openai.com:8000/v1/chat/completions"


# ── helpers ────────────────────────────────────────────────────────────────

def _orch() -> httpx.Client:
    return httpx.Client(base_url=ORCH, timeout=15.0)


def _proxy(bearer: str) -> httpx.Client:
    return httpx.Client(
        proxy=PROXY,
        headers={"X-Amaze-Bearer": bearer},
        verify=False,
        timeout=15.0,
    )


def register(agent_id: str, *, a2a_host: str = "", a2a_port: int = 0) -> tuple[str, str]:
    """Register an agent; return (bearer_token, session_id)."""
    payload: dict = {"agent_id": agent_id}
    if a2a_host:
        payload["a2a_host"] = a2a_host
        payload["a2a_port"] = a2a_port
    with _orch() as c:
        resp = c.post("/register", json=payload)
        resp.raise_for_status()
        d = resp.json()
    return d["bearer_token"], d["session_id"]


def tool_call(bearer: str, tool: str, host: str = MCP_HOST) -> httpx.Response:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }
    with _proxy(bearer) as c:
        return c.post(f"http://{host}:8000/mcp", json=body)


def llm_call(bearer: str) -> httpx.Response:
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
    }
    with _proxy(bearer) as c:
        return c.post(LLM_URL, json=body)


def a2a_call(bearer: str, target: str = A2A_CALLEE) -> httpx.Response:
    with _proxy(bearer) as c:
        return c.post(f"http://{target}:8000/", json={"task": "test"})


def audit_wait(
    r: redis.Redis,
    agent_id: str,
    n: int = 1,
    timeout: float = 8.0,
) -> list:
    """Poll until at least *n* audit records appear; return all entries."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        entries = r.xrange(f"audit:{agent_id}")
        if len(entries) >= n:
            return entries
        time.sleep(0.1)
    return r.xrange(f"audit:{agent_id}")


def clean(r: redis.Redis, agent_id: str, sid: str) -> None:
    """Wipe per-session Redis state and agent audit stream before a test."""
    for k in r.scan_iter(f"session:{sid}:*"):
        r.delete(k)
    for k in r.scan_iter(f"graph:{sid}:*"):
        r.delete(k)
    for k in r.scan_iter(f"ts:{agent_id}:*"):
        r.delete(k)
    r.delete(f"audit:{agent_id}")
    r.delete(f"trace_context:{sid}")


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def rc() -> redis.Redis:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    yield r
    r.close()


@pytest.fixture(scope="session", autouse=True)
def register_mock_mcp() -> None:
    """Register mock-mcp once so the proxy's Router can resolve it."""
    with _orch() as c:
        resp = c.post(
            "/register?kind=mcp",
            json={
                "name": MCP_HOST,
                "url": f"http://{MCP_HOST}:8000/mcp",
                "tools": ["web_search", "dummy_email"],
            },
        )
        assert resp.status_code in (200, 201, 409), (
            f"mock-mcp registration failed: {resp.text}"
        )


# ── ST-S3.1 — Remote registration ─────────────────────────────────────────

def test_st_s3_1_remote_registration(rc: redis.Redis) -> None:
    """Register an agent with explicit a2a_host + a2a_port.

    Verifies:
      - Orchestrator accepts the registration (201)
      - Redis key agent:{id}:endpoint is set with the correct URL
      - GET /resolve/agent/{id} returns 200 with the correct endpoint
      - GET /health returns 200
    """
    agent_id = "test-s3-callee"
    a2a_host = "mock-agent"
    a2a_port = 8000
    expected_endpoint = f"http://{a2a_host}:{a2a_port}"

    # Register (may return 200 if conftest.py already registered it — that's fine)
    with _orch() as c:
        resp = c.post(
            "/register",
            json={
                "agent_id": agent_id,
                "a2a_host": a2a_host,
                "a2a_port": a2a_port,
            },
        )
    assert resp.status_code in (200, 201), (
        f"registration should succeed: {resp.status_code} {resp.text}"
    )
    data = resp.json()
    assert data.get("agent_id") == agent_id

    # Redis key must be set
    endpoint_key = f"agent:{agent_id}:endpoint"
    stored = rc.get(endpoint_key)
    assert stored == expected_endpoint, (
        f"Redis key {endpoint_key!r} should be {expected_endpoint!r}, got {stored!r}"
    )

    # /resolve/agent/{id} must return the endpoint
    with _orch() as c:
        resolve = c.get(f"/resolve/agent/{agent_id}")
    assert resolve.status_code == 200, (
        f"/resolve/agent/{agent_id} expected 200, got {resolve.status_code}: {resolve.text}"
    )
    body = resolve.json()
    assert body.get("endpoint") == expected_endpoint, (
        f"resolve endpoint mismatch: {body}"
    )

    # /health
    with _orch() as c:
        health = c.get("/health")
    assert health.status_code == 200


def test_st_s3_1b_unregistered_resolve_404(rc: redis.Redis) -> None:
    """GET /resolve/agent/{id} for an unknown agent returns 404."""
    with _orch() as c:
        resp = c.get("/resolve/agent/definitely-not-registered-xyz")
    assert resp.status_code == 404
    assert resp.json().get("detail") == "agent-not-registered", resp.json()


# ── ST-S3.2 — Agent → MCP allowed ─────────────────────────────────────────

def test_st_s3_2_mcp_tool_allowed(rc: redis.Redis) -> None:
    """Router correctly routes MCP call; web_search is in policy → 200.

    The MCP endpoint is registered in Redis as `mcp:{MCP_HOST}`. The Router
    reads that key and rewrites the connection. This test verifies the full
    chain: PolicyEnforcer allows + Router routes + upstream responds.
    """
    bearer, sid = register("test-s3-mcp-only")
    clean(rc, "test-s3-mcp-only", sid)

    r = tool_call(bearer, "web_search")
    assert r.status_code == 200, f"allowed tool should be 200: {r.text}"

    entries = audit_wait(rc, "test-s3-mcp-only")
    assert entries, "audit record must exist"
    f = dict(entries[0][1])

    assert f["kind"] == "mcp",            f"kind should be mcp: {f}"
    assert f["tool"] == "web_search",     f"tool should be web_search: {f}"
    assert f["denied"] == "false",        f"should not be denied: {f}"
    assert f["trace_id"],                 f"trace_id must be set: {f}"

    # Tool counter must be incremented (Counters addon writes on response)
    time.sleep(0.3)
    ts_key = "ts:test-s3-mcp-only:tool_calls"
    assert _ts_has_data(rc, ts_key), \
        f"tool_calls counter key {ts_key!r} must have data after allowed call"


def _ts_range_sum(r: redis.Redis, key: str) -> int:
    """Sum all TS.RANGE values for a key (returns 0 if key missing)."""
    try:
        points = r.ts().range(key, "-", "+")
        return sum(int(v) for _, v in points)
    except Exception:
        return 0


def _ts_has_data(r: redis.Redis, key: str) -> bool:
    """Return True if the TS key exists and has at least one data point."""
    try:
        points = r.ts().range(key, "-", "+")
        return len(points) > 0
    except Exception:
        return False


# ── ST-S3.3 — Agent → MCP denied tool ────────────────────────────────────

def test_st_s3_3_mcp_tool_denied(rc: redis.Redis) -> None:
    """dummy_email is NOT in test-s3-mcp-only's allowed_tools → 403.

    PolicyEnforcer denies before Router fires — upstream mock-mcp is NOT hit.
    """
    bearer, sid = register("test-s3-mcp-only")
    clean(rc, "test-s3-mcp-only", sid)

    r = tool_call(bearer, "dummy_email")
    assert r.status_code == 403, f"denied tool should be 403: {r.text}"
    body = r.json()
    assert body.get("reason") == "tool-not-allowed", (
        f"reason should be tool-not-allowed: {body}"
    )

    entries = audit_wait(rc, "test-s3-mcp-only")
    assert entries, "audit record must be written even for denied calls"
    f = dict(entries[0][1])
    assert f["denied"] == "true",                         f"record must be denied: {f}"
    assert f["denial_reason"] == "tool-not-allowed",      f"denial reason: {f}"


# ── ST-S3.4 — Agent → Agent allowed (Router routing) ─────────────────────

def test_st_s3_4_a2a_allowed_routed(rc: redis.Redis) -> None:
    """Router looks up agent:test-s3-callee:endpoint and routes to mock-agent:8000.

    The test-s3-callee endpoint is registered in conftest.py (a2a_host=mock-agent,
    a2a_port=8000). The proxy rewrites the upstream connection accordingly.
    If the Router were absent the connection would fail (no Docker DNS alias for
    test-s3-callee on port 8000 that is distinct from mock-agent).
    """
    bearer, sid = register(A2A_CALLER)
    clean(rc, A2A_CALLER, sid)

    r = a2a_call(bearer, target=A2A_CALLEE)
    assert r.status_code == 200, f"A2A call to registered callee should be 200: {r.text}"

    entries = audit_wait(rc, A2A_CALLER)
    assert entries, "caller audit record must exist"
    f = dict(entries[0][1])
    assert f["kind"] == "a2a",           f"kind should be a2a: {f}"
    assert f["target"] == A2A_CALLEE,    f"target should be {A2A_CALLEE}: {f}"
    assert f["denied"] == "false",       f"should not be denied: {f}"
    assert f["trace_id"],                f"trace_id must be set: {f}"


def test_st_s3_4b_a2a_fails_without_endpoint(rc: redis.Redis) -> None:
    """A2A call to an agent that is in the policy but has NOT registered its
    endpoint → Router denies 503 agent-not-registered.

    This test verifies the Router's fail-closed behaviour directly.
    """
    # Register test-s3-caller (the agent making the call)
    bearer, sid = register(A2A_CALLER)
    clean(rc, A2A_CALLER, sid)

    # Temporarily delete test-s3-callee's endpoint from Redis to simulate
    # an agent that is in the policy (allowed_agents) but has NOT registered
    # its endpoint yet — the exact case the Router must deny fail-closed.
    endpoint_key = f"agent:{A2A_CALLEE}:endpoint"
    old_val = rc.get(endpoint_key)
    try:
        rc.delete(endpoint_key)

        r = a2a_call(bearer, target=A2A_CALLEE)
        assert r.status_code == 503, (
            f"A2A to unregistered endpoint should be 503: {r.status_code} {r.text}"
        )
        body = r.json()
        assert body.get("reason") == "agent-not-registered", (
            f"reason should be agent-not-registered: {body}"
        )
    finally:
        # Restore the endpoint so other tests aren't affected
        if old_val:
            rc.set(endpoint_key, old_val)


# ── ST-S3.5 — Agent → Agent denied ────────────────────────────────────────

def test_st_s3_5_a2a_denied(rc: redis.Redis) -> None:
    """test-s3-callee calls test-s3-caller — not in allowed_agents → 403.

    PolicyEnforcer denies before Router fires.
    """
    # test-s3-callee has allowed_agents: [] — cannot call anyone
    bearer, sid = register(A2A_CALLEE)
    clean(rc, A2A_CALLEE, sid)

    # target = A2A_CALLER which is NOT in test-s3-callee's allowed_agents
    r = a2a_call(bearer, target=A2A_CALLER)
    assert r.status_code == 403, f"disallowed A2A call should be 403: {r.text}"
    body = r.json()
    assert body.get("reason") in ("host-not-allowed", "not-allowed"), (
        f"reason should be host-not-allowed or not-allowed: {body}"
    )

    entries = audit_wait(rc, A2A_CALLEE)
    assert entries, "audit record must be written for denied A2A"
    f = dict(entries[0][1])
    assert f["denied"] == "true",  f"record must be denied: {f}"


# ── ST-S3.6 — Agent → LLM allowed ─────────────────────────────────────────

def test_st_s3_6_llm_allowed(rc: redis.Redis) -> None:
    """Router is a no-op for LLM; mitmproxy forwards to real provider (mock-llm).

    test-a2a-callee has allowed_llm_providers: [openai]. Token counter must
    be updated after the call.
    """
    bearer, sid = register("test-a2a-callee")
    clean(rc, "test-a2a-callee", sid)

    r = llm_call(bearer)
    assert r.status_code == 200, f"LLM call should be 200: {r.text}"

    entries = audit_wait(rc, "test-a2a-callee")
    assert entries, "LLM audit record must exist"
    f = dict(entries[0][1])
    assert f["kind"] == "llm",    f"kind should be llm: {f}"
    assert f["denied"] == "false", f"should not be denied: {f}"
    assert f["trace_id"],          f"trace_id must be set: {f}"

    # Token counter must be non-zero (mock-llm returns total_tokens=25)
    time.sleep(0.3)
    ts_key = "ts:test-a2a-callee:llm_tokens"
    assert _ts_has_data(rc, ts_key), (
        f"llm_tokens counter key {ts_key!r} must have data after LLM call"
    )
    total = _ts_range_sum(rc, ts_key)
    assert total > 0, f"llm_tokens should be > 0 after call, got {total}"


# ── ST-S3.7 — Agent → LLM denied ──────────────────────────────────────────

def test_st_s3_7_llm_denied(rc: redis.Redis) -> None:
    """test-strict-loop has allowed_llm_providers:[] → LLM call → 403 llm-not-allowed.

    PolicyEnforcer denies before the Router fires (kind is never set to 'llm'
    for a denied flow, so Router remains a complete no-op).
    """
    bearer, sid = register("test-strict-loop")
    clean(rc, "test-strict-loop", sid)

    r = llm_call(bearer)
    assert r.status_code == 403, f"LLM call to no-LLM policy should be 403: {r.text}"
    body = r.json()
    assert body.get("reason") == "llm-not-allowed", (
        f"reason should be llm-not-allowed: {body}"
    )

    entries = audit_wait(rc, "test-strict-loop")
    assert entries, "audit record must exist for denied LLM call"
    f = dict(entries[0][1])
    assert f["denied"] == "true",                        f"record must be denied: {f}"
    assert f["denial_reason"] == "llm-not-allowed",      f"denial reason: {f}"
