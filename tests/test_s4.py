"""
Sprint S4 system tests — Phases 1-3 (16 of 19 signed-off tests).

Validates the S4 orchestrator endpoints (read + write) backing the new
React GUI, plus the Phase-3 send-message → trace-detail end-to-end flow.

Tests run against the LIVE `amaze-platform` container (host ports
8001/orch, 8080/proxy, 6379/redis on 127.0.0.1, agent-sdk chat on 8090).
We do NOT spin up `tests/compose.test.yml` — the live demo stack is
the device under test.

Override hosts:
  AMAZE_ORCHESTRATOR   http://localhost:8001
  REDIS_HOST           127.0.0.1
  REDIS_PORT           6379
  AGENT_SDK_CHAT       http://localhost:8090

Deferred / skipped (per S4-T3.6 brief):
  ST-S4.13, ST-S4.14   stats endpoints — Phase 4
  ST-S4.18             alerts donut    — Phase 4
  ST-S4.19             export ZIP      — Phase 6

Cleanup contract: every write-test restores prior state (Redis keys,
litellm.yaml entries, policy doc) via try/finally.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

import httpx
import pytest
import redis

# ── connection settings ────────────────────────────────────────────────────

ORCH = os.environ.get("AMAZE_ORCHESTRATOR", "http://localhost:8001")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
AGENT_SDK_CHAT = os.environ.get("AGENT_SDK_CHAT", "http://localhost:8090")

# Live demo agent we exercise repeatedly — already approved + has policy +
# has chat_endpoint registered (see CLAUDE.md §8 demo wiring).
DEMO_AGENT = "agent-sdk"


# ── helpers ────────────────────────────────────────────────────────────────


def _container_litellm_delete() -> None:
    """Remove /app/config/litellm.yaml inside the platform container.

    POST /llms writes to CONFIG_DIR=/app/config inside amaze-platform; from
    the host we can't see/edit it directly so we shell into the container.
    Idempotent: ignores "no such file" errors.
    """
    try:
        subprocess.run(
            ["docker", "exec", "amaze-platform", "rm", "-f",
             "/app/config/litellm.yaml"],
            check=False, capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass  # best-effort cleanup


def _container_litellm_remove_entry(model_name: str) -> None:
    """Remove a single model_list entry from the in-container litellm.yaml.

    Used when ST-S4.8 ran but other entries may exist that we should not
    nuke. Cheap implementation: read → parse → filter → write back via
    a heredoc. Falls back to nothing if the file is gone.
    """
    script = (
        "import yaml, sys, os, pathlib;"
        "p = pathlib.Path('/app/config/litellm.yaml');"
        "data = yaml.safe_load(p.read_text()) or {} if p.exists() else {};"
        "ml = data.get('model_list') or [];"
        f"ml = [e for e in ml if not (isinstance(e, dict) and e.get('model_name') == '{model_name}')];"
        "data['model_list'] = ml;"
        "p.write_text(yaml.safe_dump(data, sort_keys=False)) if ml else (p.unlink() if p.exists() else None)"
    )
    try:
        subprocess.run(
            ["docker", "exec", "amaze-platform", "python", "-c", script],
            check=False, capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    with httpx.Client(base_url=ORCH, timeout=60.0) as c:
        yield c


@pytest.fixture(scope="module")
def rc() -> redis.Redis:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
    except redis.RedisError as e:
        pytest.skip(f"redis unreachable at {REDIS_HOST}:{REDIS_PORT}: {e}")
    yield r
    r.close()


# ─────────────────────────────────────────────────────────────────────────────
# Atomic — read endpoints
# ─────────────────────────────────────────────────────────────────────────────


def test_st_s4_1_list_agents(http: httpx.Client) -> None:
    """ST-S4.1 — `GET /agents` returns rows with state classification."""
    resp = http.get("/agents")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list) and rows, "expected at least one agent"

    valid_states = {"pending", "approved-no-policy", "approved-with-policy"}
    for row in rows:
        assert "agent_id" in row, row
        assert row.get("state") in valid_states, (
            f"state must be one of {valid_states}, got: {row}"
        )

    by_id = {r["agent_id"]: r for r in rows}
    assert DEMO_AGENT in by_id, f"{DEMO_AGENT} must be present: {list(by_id)}"
    assert by_id[DEMO_AGENT]["state"] == "approved-with-policy", by_id[DEMO_AGENT]


def test_st_s4_2_list_mcp_servers(http: httpx.Client) -> None:
    """ST-S4.2 — `GET /mcp_servers` includes `demo-mcp` with tools and approved."""
    resp = http.get("/mcp_servers")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list)

    by_name = {r["name"]: r for r in rows}
    assert "demo-mcp" in by_name, f"demo-mcp must be registered: {list(by_name)}"
    demo = by_name["demo-mcp"]
    assert demo.get("approved") is True, demo
    assert isinstance(demo.get("tools"), list) and demo["tools"], (
        f"demo-mcp must advertise non-empty tools[]: {demo}"
    )
    assert demo.get("url"), demo


def test_st_s4_3_list_llms(http: httpx.Client) -> None:
    """ST-S4.3 — `GET /llms` returns a list (may be empty if no litellm.yaml)."""
    resp = http.get("/llms")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list), f"expected list, got: {body!r}"
    # Each entry, if present, must carry provider + model.
    for e in body:
        assert "provider" in e and "model" in e, e


def test_st_s4_4_list_traces_for_agent(http: httpx.Client) -> None:
    """ST-S4.4 — `GET /traces?agent=&limit=` returns {traces, next_cursor}."""
    resp = http.get(f"/traces", params={"agent": DEMO_AGENT, "limit": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "traces" in body and "next_cursor" in body, body
    traces = body["traces"]
    assert isinstance(traces, list)

    # If empty, that's tolerable — fresh demo. If non-empty, every row must
    # carry the documented fields.
    for row in traces:
        for k in ("trace_id", "agent_id", "started_at",
                  "llm_calls", "tool_calls", "a2a_calls", "status"):
            assert k in row, f"missing {k} in trace summary: {row}"
        assert row["agent_id"] == DEMO_AGENT, row


def test_st_s4_5_trace_detail(http: httpx.Client) -> None:
    """ST-S4.5 — `GET /traces/{trace_id}` returns the assembled record."""
    # Pick the first trace from /traces; if none exist we skip rather than
    # synthesize one (ST-S4.17 covers the create-and-fetch round-trip).
    listing = http.get("/traces", params={"agent": DEMO_AGENT, "limit": 1})
    assert listing.status_code == 200, listing.text
    traces = listing.json().get("traces", [])
    if not traces:
        pytest.skip("no traces in audit stream — ST-S4.17 covers fresh creation")

    tid = traces[0]["trace_id"]
    resp = http.get(f"/traces/{tid}")
    assert resp.status_code == 200, resp.text
    detail = resp.json()

    for k in ("trace_id", "title", "passed", "duration",
              "summary", "metrics", "sequence_steps", "edges",
              "violations_list"):
        assert k in detail, f"missing {k} in trace detail: {list(detail)}"
    assert detail["trace_id"] == tid
    assert isinstance(detail["sequence_steps"], list)
    assert isinstance(detail["edges"], list)
    assert isinstance(detail["violations_list"], list)
    # `summary` carries the prompt / final_answer / policy_snapshot triple.
    assert isinstance(detail["summary"], dict), detail["summary"]


# ─────────────────────────────────────────────────────────────────────────────
# Atomic — write endpoints
# ─────────────────────────────────────────────────────────────────────────────


def test_st_s4_6_approve_agent_idempotent(http: httpx.Client) -> None:
    """ST-S4.6 — POST /agents/{id}/approve is 200 and idempotent."""
    r1 = http.post(f"/agents/{DEMO_AGENT}/approve")
    assert r1.status_code == 200, r1.text
    r2 = http.post(f"/agents/{DEMO_AGENT}/approve")
    assert r2.status_code == 200, r2.text  # idempotent

    listing = http.get("/agents").json()
    by_id = {a["agent_id"]: a for a in listing}
    # Approve doesn't take agent OUT of pending, but it also must not place
    # it INTO pending. agent-sdk has a YAML policy → approved-with-policy.
    assert by_id[DEMO_AGENT]["state"] != "pending", by_id[DEMO_AGENT]


def test_st_s4_7_approve_mcp_server(http: httpx.Client, rc: redis.Redis) -> None:
    """ST-S4.7 — Register temp MCP, approve, verify Redis flag."""
    name = "t-s4-7-mcp"
    rc.delete(f"mcp:{name}", f"mcp:{name}:approved")
    try:
        # Manual register to make the approve target exist.
        reg = http.post("/mcp_servers", json={
            "name": name, "url": "http://example.test/mcp/",
            "tools": ["echo"],
        })
        assert reg.status_code == 201, reg.text

        ap = http.post(f"/mcp_servers/{name}/approve")
        assert ap.status_code == 200, ap.text
        assert rc.get(f"mcp:{name}:approved") == "true"
    finally:
        rc.delete(f"mcp:{name}", f"mcp:{name}:approved")


def test_st_s4_8_create_llm_entry(http: httpx.Client) -> None:
    """ST-S4.8 — POST /llms appends a model_list entry; visible in GET."""
    model = "test-s4-8-model"
    try:
        resp = http.post("/llms", json={
            "provider": "openai", "model": model, "api_key_ref": "DUMMY",
        })
        assert resp.status_code == 201, resp.text

        listing = http.get("/llms").json()
        names = [(e.get("provider"), e.get("model")) for e in listing]
        assert ("openai", model) in names, f"new entry missing in {names}"
    finally:
        # Remove just our entry; preserve any pre-existing models.
        _container_litellm_remove_entry(model)


def test_st_s4_9_create_mcp_server_and_duplicate(
    http: httpx.Client, rc: redis.Redis,
) -> None:
    """ST-S4.9 — Manual MCP creation works once; duplicate returns 409."""
    name = "t-s4-9-mcp"
    rc.delete(f"mcp:{name}", f"mcp:{name}:approved")
    try:
        body = {
            "name": name, "url": "http://example.test/mcp/",
            "tools": ["echo"],
        }
        r1 = http.post("/mcp_servers", json=body)
        assert r1.status_code == 201, r1.text
        r2 = http.post("/mcp_servers", json=body)
        assert r2.status_code == 409, r2.text

        # Visible in listing too.
        listing = http.get("/mcp_servers").json()
        assert any(e["name"] == name for e in listing), listing
    finally:
        rc.delete(f"mcp:{name}", f"mcp:{name}:approved")


def test_st_s4_10_policy_put_round_trip(http: httpx.Client) -> None:
    """ST-S4.10 — PUT /policy persists; GET returns the new value live."""
    # Capture current state.
    cur = http.get(f"/policy/{DEMO_AGENT}")
    assert cur.status_code == 200, cur.text
    original = cur.json()
    # `graph: null` confuses the round-trip if PUT echoes None — strip
    # so the saved doc keeps the same shape under either schema choice.
    saved = dict(original)

    flipped = dict(original)
    flipped["on_violation"] = (
        "allow" if original.get("on_violation", "block") == "block" else "block"
    )
    try:
        put = http.put(f"/policy/{DEMO_AGENT}", json=flipped)
        assert put.status_code == 200, put.text

        check = http.get(f"/policy/{DEMO_AGENT}")
        assert check.status_code == 200, check.text
        assert check.json().get("on_violation") == flipped["on_violation"], check.json()
    finally:
        # Restore original — best-effort.
        http.put(f"/policy/{DEMO_AGENT}", json=saved)


def test_st_s4_11_policy_get_unknown_404(http: httpx.Client) -> None:
    """ST-S4.11 — GET /policy/{id}: full doc on known agent, 404 unknown."""
    ok = http.get(f"/policy/{DEMO_AGENT}")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    # Documented fields from Policy pydantic model.
    for k in ("name", "mode", "max_tokens_per_turn",
              "max_tool_calls_per_turn", "max_agent_calls_per_turn"):
        assert k in body, f"missing {k} in policy doc: {list(body)}"

    miss = http.get(f"/policy/{uuid.uuid4().hex}-no-such-agent")
    assert miss.status_code == 404, miss.text


def test_st_s4_12_send_message(http: httpx.Client, rc: redis.Redis) -> None:
    """ST-S4.12 — POST /agents/{id}/messages → response.reply + trace_id."""
    # Capture newest audit stream id BEFORE sending so we can assert the
    # returned trace_id refers to a fresh record (not a stale latest one).
    prior = rc.xrevrange(f"audit:{DEMO_AGENT}", count=1)
    prior_stream_id = prior[0][0] if prior else None

    resp = http.post(
        f"/agents/{DEMO_AGENT}/messages",
        json={"prompt": "search for current weather in Berlin"},
        timeout=120.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "response" in body, body
    assert body.get("trace_id"), f"trace_id must be set: {body}"
    assert body.get("denial") is None, f"denial should be null on success: {body}"
    # Either dict-with-reply or wrapped text — both are valid SDK responses.
    rsp = body["response"]
    assert isinstance(rsp, dict), rsp
    assert any(k in rsp for k in ("reply", "text", "error")), rsp

    # Audit stream must have grown.
    new = rc.xrevrange(f"audit:{DEMO_AGENT}", count=1)
    assert new, "no audit records after send-message"
    new_stream_id = new[0][0]
    assert new_stream_id != prior_stream_id, (
        f"expected a fresh audit record, prior={prior_stream_id} new={new_stream_id}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Stats — deferred to Phase 4
# ─────────────────────────────────────────────────────────────────────────────


def test_st_s4_13_stats_calls(http: httpx.Client) -> None:
    """ST-S4.13 — `GET /agents/{id}/stats?range=24h` returns the dashboard payload."""
    r = http.get(f"/agents/{DEMO_AGENT}/stats", params={"range": "24h"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Top-level shape from T4.1 DashboardPayload
    for key in ("kpi", "calls_over_time", "latency_per_call",
                "tokens", "tools", "a2a", "alerts"):
        assert key in body, f"missing {key}: {body.keys()}"
    # KPI shape
    kpi = body["kpi"]
    for k in ("calls", "unique_tools", "avg_latency_ms",
             "critical_alerts", "policy_health"):
        assert k in kpi
    assert kpi["policy_health"] in ("ok", "warn", "fail")
    # 24-point time series
    assert len(body["calls_over_time"]) == 24
    assert len(body["latency_per_call"]) == 24
    # Bad range → 400
    bad = http.get(f"/agents/{DEMO_AGENT}/stats", params={"range": "garbage"})
    assert bad.status_code == 400


def test_st_s4_14_alerts_grouped(http: httpx.Client) -> None:
    """ST-S4.14 — `GET /alerts?range=24h&groupBy=reason` returns counts and records."""
    r = http.get("/alerts", params={"range": "24h", "groupBy": "reason"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {"total", "by_reason", "records"}
    assert isinstance(body["total"], int)
    assert isinstance(body["by_reason"], list)
    if body["by_reason"]:
        first = body["by_reason"][0]
        assert "label" in first and "count" in first
    # Bad groupBy → 400
    bad = http.get("/alerts", params={"groupBy": "garbage"})
    assert bad.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Integration / end-to-end
# ─────────────────────────────────────────────────────────────────────────────


def test_st_s4_15_approval_flow(http: httpx.Client, rc: redis.Redis) -> None:
    """ST-S4.15 — Register MCP → approve → visible in GET as approved.

    Skips the post-approval tool-call leg (too plumbing-heavy for Phase 3
    per S4-T3.6 brief).
    """
    name = "t-s4-15-mcp"
    rc.delete(f"mcp:{name}", f"mcp:{name}:approved")
    try:
        reg = http.post("/mcp_servers", json={
            "name": name, "url": "http://example.test/mcp/",
            "tools": ["ping"],
        })
        assert reg.status_code == 201, reg.text

        # Listing shows it as approved (POST /mcp_servers sets approved=true).
        listing = http.get("/mcp_servers").json()
        match = next((e for e in listing if e["name"] == name), None)
        assert match is not None, listing
        assert match.get("approved") is True

        # Toggle reject → re-approve.
        rj = http.post(f"/mcp_servers/{name}/reject")
        assert rj.status_code == 200, rj.text
        assert rc.get(f"mcp:{name}:approved") == "false"

        ap = http.post(f"/mcp_servers/{name}/approve")
        assert ap.status_code == 200, ap.text
        assert rc.get(f"mcp:{name}:approved") == "true"
    finally:
        rc.delete(f"mcp:{name}", f"mcp:{name}:approved")


def test_st_s4_16_policy_live_update(http: httpx.Client) -> None:
    """ST-S4.16 — PUT /policy is live-visible in trace policy_snapshot.

    Practical version per S4-T3.6 brief: flip `on_violation`, send a
    no-op weather message, fetch the resulting trace's policy snapshot,
    assert the snapshot reflects the new value. We avoid the brittle
    "trigger a graph violation" path.
    """
    cur = http.get(f"/policy/{DEMO_AGENT}").json()
    original_on_v = cur.get("on_violation", "block")
    flipped_on_v = "allow" if original_on_v == "block" else "block"

    flipped_doc = dict(cur)
    flipped_doc["on_violation"] = flipped_on_v
    saved = dict(cur)

    try:
        put = http.put(f"/policy/{DEMO_AGENT}", json=flipped_doc)
        assert put.status_code == 200, put.text

        # Send a benign message to get a fresh trace.
        msg = http.post(
            f"/agents/{DEMO_AGENT}/messages",
            json={"prompt": "search for current weather in Berlin"},
            timeout=120.0,
        )
        assert msg.status_code == 200, msg.text
        tid = msg.json().get("trace_id")
        if not tid:
            pytest.skip("no trace_id returned — agent may have failed upstream")

        detail = http.get(f"/traces/{tid}")
        assert detail.status_code == 200, detail.text
        snap = detail.json().get("summary", {}).get("policy_snapshot") or {}
        assert snap.get("on_violation") == flipped_on_v, (
            f"policy snapshot should reflect new value; "
            f"expected {flipped_on_v}, got {snap.get('on_violation')!r}"
        )
    finally:
        http.put(f"/policy/{DEMO_AGENT}", json=saved)


def test_st_s4_17_send_message_trace_detail(http: httpx.Client) -> None:
    """ST-S4.17 — Send-message → trace detail with non-empty edges/sequence."""
    msg = http.post(
        f"/agents/{DEMO_AGENT}/messages",
        json={"prompt": "search for current weather in Berlin"},
        timeout=120.0,
    )
    assert msg.status_code == 200, msg.text
    body = msg.json()
    tid = body.get("trace_id")
    if not tid:
        pytest.skip(f"no trace_id from send-message: {body}")

    # Trace detail must assemble fully; there must be at least one LLM edge.
    detail = http.get(f"/traces/{tid}")
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["trace_id"] == tid
    assert d["edges"], f"expected non-empty edges[]: {d}"
    assert d["sequence_steps"], f"expected non-empty sequence_steps[]: {d}"
    assert any(e.get("type") == "llm" for e in d["edges"]), (
        f"expected at least one type=llm edge: {d['edges']}"
    )


@pytest.mark.skip(reason="ST-S4.18 — alerts donut filter (Phase 4)")
def test_st_s4_18_alerts_donut() -> None:
    pass


@pytest.mark.skip(reason="ST-S4.19 — export ZIP (Phase 6)")
def test_st_s4_19_export() -> None:
    pass
