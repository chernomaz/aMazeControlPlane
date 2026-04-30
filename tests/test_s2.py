"""
Sprint S2 system tests — ST-S2.1 through ST-S2.13.

Prerequisites
─────────────
  docker compose -f tests/compose.test.yml up --build -d
  pip install -r tests/requirements.txt
  pytest tests/test_s2.py -v

Environment overrides (all optional):
  AMAZE_PROXY        http://localhost:18080
  AMAZE_ORCHESTRATOR http://localhost:18001
  REDIS_HOST         localhost
  REDIS_PORT         16379
  JAEGER_URL         http://localhost:16686

Every test verifies:
  1. Correct HTTP status + body from the proxy.
  2. Matching record(s) in the Redis audit stream.
  3. Trace ID presence / cross-agent equality where applicable.

Tests are independent: each registers a fresh agent session and clears
prior Redis state for that agent before running.
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
ORCH = os.environ.get("AMAZE_ORCHESTRATOR", "http://localhost:18001")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "16379"))
JAEGER = os.environ.get("JAEGER_URL", "http://localhost:16686")

MCP_HOST = "mock-mcp"          # hostname inside Docker amaze-mcp-net
A2A_HOST = "test-a2a-callee"   # mock-agent alias inside amaze-agent-net
# LLM calls use HTTP to port 8000; mock-llm is aliased as api.openai.com on
# amaze-egress so the proxy forwards there.  pretty_host still = api.openai.com,
# which is_llm_host() recognises.
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


def register(agent_id: str) -> tuple[str, str]:
    """Register an agent; return (bearer_token, session_id)."""
    with _orch() as c:
        resp = c.post("/register", json={"agent_id": agent_id})
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


def a2a_call(bearer: str, target: str = A2A_HOST) -> httpx.Response:
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
    """Register mock-mcp once so the proxy knows it's a valid MCP server."""
    with _orch() as c:
        resp = c.post(
            "/register?kind=mcp",
            json={
                "name": MCP_HOST,
                "url": f"http://{MCP_HOST}:8000/mcp",
                "tools": ["web_search", "dummy_email"],
            },
        )
        # 201 on first register; may be 200/409 on repeated runs — both fine.
        assert resp.status_code in (200, 201, 409), (
            f"mock-mcp registration failed: {resp.text}"
        )


# ── ST-S2.1 ────────────────────────────────────────────────────────────────

def test_s2_1_strict_correct_sequence(rc: redis.Redis) -> None:
    """Strict policy: correct sequence (web_search → dummy_email) allowed."""
    bearer, sid = register("test-strict")
    clean(rc, "test-strict", sid)

    r1 = tool_call(bearer, "web_search")
    assert r1.status_code == 200, f"web_search (step 1) should be 200: {r1.text}"

    r2 = tool_call(bearer, "dummy_email")
    assert r2.status_code == 200, f"dummy_email (step 2) should be 200: {r2.text}"

    entries = audit_wait(rc, "test-strict", n=2)
    assert len(entries) >= 2, "expected ≥2 audit records"

    f1 = dict(entries[0][1])
    f2 = dict(entries[1][1])

    assert f1["tool"] == "web_search", f"first record should be web_search, got {f1['tool']}"
    assert f2["tool"] == "dummy_email", f"second record should be dummy_email, got {f2['tool']}"
    assert f1["denied"] == "false"
    assert f2["denied"] == "false"
    assert f1["trace_id"], "trace_id should be non-empty for allowed request"
    assert f2["trace_id"], "trace_id should be non-empty for allowed request"


# ── ST-S2.2 ────────────────────────────────────────────────────────────────

def test_s2_2_strict_wrong_order(rc: redis.Redis) -> None:
    """Strict policy: calling dummy_email before web_search → 403 graph_violation."""
    bearer, sid = register("test-strict")
    clean(rc, "test-strict", sid)

    r = tool_call(bearer, "dummy_email")  # wrong first step
    assert r.status_code == 403, f"wrong-order call should be 403: {r.text}"
    assert r.json()["reason"] == "graph_violation"

    entries = audit_wait(rc, "test-strict")
    assert entries, "audit record should exist for denied request"
    f = dict(entries[0][1])
    assert f["denied"] == "true"
    assert f["denial_reason"] == "graph_violation"


# ── ST-S2.3 ────────────────────────────────────────────────────────────────

def test_s2_3_flexible_any_order(rc: redis.Redis) -> None:
    """Flexible policy: tools may be called in any order."""
    bearer, sid = register("test-flexible")
    clean(rc, "test-flexible", sid)

    r1 = tool_call(bearer, "dummy_email")
    assert r1.status_code == 200, f"dummy_email first should be allowed: {r1.text}"

    r2 = tool_call(bearer, "web_search")
    assert r2.status_code == 200, f"web_search second should be allowed: {r2.text}"

    entries = audit_wait(rc, "test-flexible", n=2)
    assert len(entries) >= 2
    f1, f2 = dict(entries[0][1]), dict(entries[1][1])
    assert f1["tool"] == "dummy_email"
    assert f2["tool"] == "web_search"
    assert f1["denied"] == "false"
    assert f2["denied"] == "false"


# ── ST-S2.4 ────────────────────────────────────────────────────────────────

def test_s2_4_flexible_unlisted_tool(rc: redis.Redis) -> None:
    """Flexible policy: tool not in allowed_tools → 403 tool-not-allowed."""
    bearer, sid = register("test-flexible")
    clean(rc, "test-flexible", sid)

    r = tool_call(bearer, "delete_all")
    assert r.status_code == 403, f"unlisted tool should be 403: {r.text}"
    assert r.json()["reason"] == "tool-not-allowed"

    entries = audit_wait(rc, "test-flexible")
    assert entries
    f = dict(entries[0][1])
    assert f["denied"] == "true"
    assert f["denial_reason"] == "tool-not-allowed"


# ── ST-S2.5 ────────────────────────────────────────────────────────────────

def test_s2_5_allow_mode_violation_passes(rc: redis.Redis) -> None:
    """on_violation:allow — wrong order passes through; violation in alert field."""
    bearer, sid = register("test-strict-allow")
    clean(rc, "test-strict-allow", sid)

    r = tool_call(bearer, "dummy_email")  # wrong first step, but allow-mode
    assert r.status_code == 200, f"allow-mode violation should not block: {r.text}"

    entries = audit_wait(rc, "test-strict-allow")
    assert entries
    f = dict(entries[0][1])
    assert f["denied"] == "false", "request should not be denied in allow mode"
    assert f["alert"], "violation should be written to alert field"
    alert_data = json.loads(f["alert"])
    assert alert_data.get("reason") == "graph_violation"


# ── ST-S2.6 ────────────────────────────────────────────────────────────────

def test_s2_6_loop_limit(rc: redis.Redis) -> None:
    """Loop limit: max_loops:1 exhausted → second call returns 429 edge_loop_exceeded."""
    bearer, sid = register("test-strict-loop")
    clean(rc, "test-strict-loop", sid)

    r1 = tool_call(bearer, "web_search")
    assert r1.status_code == 200, f"first call should be allowed: {r1.text}"

    # After first call graph moves to "done" (max_loops=1, next_steps=[]).
    r2 = tool_call(bearer, "web_search")
    assert r2.status_code == 429, f"second call should be 429: {r2.text}"
    assert r2.json()["reason"] == "edge_loop_exceeded"

    entries = audit_wait(rc, "test-strict-loop", n=2)
    assert len(entries) >= 2
    f1, f2 = dict(entries[0][1]), dict(entries[1][1])
    assert f1["denied"] == "false"
    assert f2["denied"] == "true"
    assert f2["denial_reason"] == "edge_loop_exceeded"


# ── ST-S2.7 ────────────────────────────────────────────────────────────────

def test_s2_7_token_budget_per_turn(rc: redis.Redis) -> None:
    """max_tokens_per_turn:50 — pre-seeded counter at limit blocks LLM call."""
    bearer, sid = register("test-low-tokens")
    clean(rc, "test-low-tokens", sid)

    # Pre-seed counter at the limit so the next LLM request is denied.
    rc.set(f"session:{sid}:total_tokens", 50)

    r = llm_call(bearer)
    assert r.status_code == 403, f"budget-exceeded LLM call should be 403: {r.text}"
    assert r.json()["reason"] == "budget_exceeded"

    entries = audit_wait(rc, "test-low-tokens")
    assert entries
    f = dict(entries[0][1])
    assert f["denied"] == "true"
    assert f["denial_reason"] == "budget_exceeded"
    assert f["kind"] == "llm"


# ── ST-S2.8 ────────────────────────────────────────────────────────────────

def test_s2_8_tool_call_limit(rc: redis.Redis) -> None:
    """max_tool_calls_per_turn:1 — second tool call returns 403 tool-limit-exceeded."""
    bearer, sid = register("test-low-tools")
    clean(rc, "test-low-tools", sid)

    r1 = tool_call(bearer, "web_search")
    assert r1.status_code == 200, f"first tool call should succeed: {r1.text}"

    # Counters.response() INCR happens synchronously before the HTTP response
    # returns, so no sleep is needed.  Small guard against edge-case timing:
    time.sleep(0.2)

    r2 = tool_call(bearer, "dummy_email")
    assert r2.status_code == 403, f"second tool call should be 403: {r2.text}"
    assert r2.json()["reason"] == "tool-limit-exceeded"

    entries = audit_wait(rc, "test-low-tools", n=2)
    assert len(entries) >= 2
    f2 = dict(entries[-1][1])
    assert f2["denied"] == "true"
    assert f2["denial_reason"] == "tool-limit-exceeded"


# ── ST-S2.9 ────────────────────────────────────────────────────────────────

def test_s2_9_rate_limit_window(rc: redis.Redis) -> None:
    """Rate limit: pre-seeded TS data at cap → LLM call denied rate-limit-exceeded."""
    bearer, sid = register("test-rate-limit")
    clean(rc, "test-rate-limit", sid)

    # Seed TS key with 50 tokens within the last 10 seconds (inside 1-min window).
    now_ms = int(time.time() * 1000)
    ts_key = "ts:test-rate-limit:llm_tokens"
    rc.ts().add(ts_key, now_ms - 10_000, 50)

    r = llm_call(bearer)
    assert r.status_code == 403, f"rate-limited LLM call should be 403: {r.text}"
    assert r.json()["reason"] == "rate-limit-exceeded"

    entries = audit_wait(rc, "test-rate-limit")
    assert entries
    f = dict(entries[0][1])
    assert f["denied"] == "true"
    assert f["denial_reason"] == "rate-limit-exceeded"


# ── ST-S2.10 ───────────────────────────────────────────────────────────────

def test_s2_10_a2a_trace_propagation(rc: redis.Redis) -> None:
    """A2A + LLM: same trace_id in both agents' audit records."""
    bearer_caller, sid_caller = register("test-a2a-caller")
    bearer_callee, sid_callee = register("test-a2a-callee")
    clean(rc, "test-a2a-caller", sid_caller)
    clean(rc, "test-a2a-callee", sid_callee)

    # Caller makes A2A call → proxy allows, opens span T1, stores traceparent
    # for callee's session in Redis.
    r_a2a = a2a_call(bearer_caller, target=A2A_HOST)
    assert r_a2a.status_code == 200, f"A2A call should succeed: {r_a2a.text}"

    # Tracer must have stored the traceparent for the callee session.
    stored_tp = rc.get(f"trace_context:{sid_callee}")
    assert stored_tp, "trace_context should be stored in Redis for callee session"

    # Callee makes LLM call → proxy reads stored traceparent and opens child span.
    r_llm = llm_call(bearer_callee)
    assert r_llm.status_code == 200, f"callee LLM call should succeed: {r_llm.text}"

    caller_entries = audit_wait(rc, "test-a2a-caller")
    callee_entries = audit_wait(rc, "test-a2a-callee")
    assert caller_entries, "caller should have audit record"
    assert callee_entries, "callee should have audit record"

    caller_trace = dict(caller_entries[0][1])["trace_id"]
    callee_trace = dict(callee_entries[0][1])["trace_id"]

    assert caller_trace, "caller trace_id must be non-empty"
    assert callee_trace, "callee trace_id must be non-empty"
    assert caller_trace == callee_trace, (
        f"trace_ids must match across agents: caller={caller_trace} callee={callee_trace}"
    )


# ── ST-S2.11 ───────────────────────────────────────────────────────────────

def test_s2_11_audit_log_completeness(rc: redis.Redis) -> None:
    """Every audit record for an allowed call has all required fields populated."""
    bearer, sid = register("test-strict")
    clean(rc, "test-strict", sid)

    tool_call(bearer, "web_search")
    tool_call(bearer, "dummy_email")

    entries = audit_wait(rc, "test-strict", n=2)
    assert len(entries) >= 2

    required = {
        "trace_id", "span_id", "agent_id", "session_id",
        "kind", "target", "tool", "input", "output",
        "ts", "denied", "denial_reason",
    }
    for _, fields in entries:
        d = dict(fields)
        missing = required - d.keys()
        assert not missing, f"audit record missing fields: {missing}"
        assert d["agent_id"] == "test-strict"
        assert d["session_id"] == sid
        assert d["kind"] == "mcp"
        assert d["target"] == MCP_HOST
        assert d["ts"]
        # Allowed calls must have span context
        if d["denied"] == "false":
            assert d["trace_id"], "allowed request must have trace_id"
            assert d["span_id"], "allowed request must have span_id"
            assert d["output"], "allowed request must have non-empty output"


# ── ST-S2.12 ───────────────────────────────────────────────────────────────

def test_s2_12_redis_timeseries_metrics(rc: redis.Redis) -> None:
    """LLM call → token count written to RedisTimeSeries; TS.RANGE returns data."""
    bearer, sid = register("test-a2a-callee")
    clean(rc, "test-a2a-callee", sid)

    r = llm_call(bearer)
    assert r.status_code == 200, f"LLM call should succeed: {r.text}"

    # Give Counters.response() a moment to flush the TS write.
    time.sleep(0.3)

    now_ms = int(time.time() * 1000)
    window_start = now_ms - 60_000  # 1-minute window

    try:
        values = rc.ts().range("ts:test-a2a-callee:llm_tokens", window_start, "+")
    except Exception as exc:
        pytest.fail(f"TS.RANGE failed: {exc}")

    assert values, "RedisTimeSeries should contain llm_tokens data points"
    total = sum(v for _, v in values)
    assert total == 25, f"mock-llm returns 25 total_tokens; got {total}"


# ── ST-S2.13 ───────────────────────────────────────────────────────────────

def test_s2_13_stream_false_enforced(rc: redis.Redis) -> None:
    """StreamBlocker injects 'stream': false into every LLM request body."""
    bearer, sid = register("test-a2a-callee")
    clean(rc, "test-a2a-callee", sid)

    # Send a body WITHOUT 'stream'; the proxy should inject stream:false.
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
    }
    with _proxy(bearer) as c:
        c.post(LLM_URL, json=body)

    entries = audit_wait(rc, "test-a2a-callee")
    assert entries, "audit record should exist"

    f = dict(entries[0][1])
    raw_input = f.get("input", "")
    assert raw_input, "audit input must be non-empty for an LLM call"

    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError:
        pytest.fail(f"audit input is not valid JSON: {raw_input!r}")

    assert parsed.get("stream") is False, (
        f"proxy must inject stream:false; got stream={parsed.get('stream')!r}"
    )
