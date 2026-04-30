"""
Sprint S1 integration tests — ST-S1.10, ST-S1.11, ST-S1.12.

Validates the full stack end-to-end:
  1. Correct HTTP response from agent-sdk
  2. Audit log records present in Redis Streams
  3. Trace IDs present (and consistent across cross-agent calls)

Prerequisites:
  - Platform stack running: `docker compose -f docker/docker-compose.yml -f examples/compose.yml up -d`
  - OPENAI_API_KEY and TAVILY_API_KEY set inside the agent + mcp containers.

Run:
  /home/ubuntu/venv/bin/pytest tests/test_s1_integration.py -v -s

NOT PARALLEL-SAFE
─────────────────
The module-scoped autouse `_reset_session_counters` fixture deletes all
`session:*` keys in Redis at startup. Running two pytest processes against
the same platform concurrently will wipe each other's counters mid-test
and produce misleading budget_exceeded denials. Either:
  - Run pytest sequentially (default), or
  - Use a per-process Redis namespace (would require proxy changes).
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import httpx
import pytest

# ── connection settings ──────────────────────────────────────────────────────
AGENT_SDK = os.environ.get("AGENT_SDK_URL", "http://localhost:8090")
PLATFORM_CONTAINER = os.environ.get("PLATFORM_CONTAINER", "amaze-platform")

# Live LLM/tool round-trips can take 30s+ each.
HTTP_TIMEOUT = 90.0


# ── Redis access via docker exec (Redis is internal to platform container) ──
class RedisProxy:
    """Minimal Redis wrapper that shells into the platform container."""
    def __init__(self, container: str = PLATFORM_CONTAINER):
        self.container = container

    def _cli(self, *args: str) -> str:
        cmd = ["docker", "exec", self.container, "redis-cli", "--no-raw", *args]
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if out.returncode != 0:
            raise RuntimeError(f"redis-cli failed: {out.stderr}")
        return out.stdout

    def ping(self) -> bool:
        return self._cli("PING").strip() == "PONG"

    def xrange(self, stream: str, min_id: str = "-", max_id: str = "+") -> list[dict]:
        """Return list of {id, **fields} dicts for a stream range.

        Uses JSON output mode so we can parse reliably.
        """
        cmd = ["docker", "exec", self.container, "redis-cli", "--json",
               "XRANGE", stream, min_id, max_id]
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if out.returncode != 0 or not out.stdout.strip():
            return []
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            return []
        # data is [[id, [k1, v1, k2, v2, ...]], ...]
        records = []
        for entry in data or []:
            if not isinstance(entry, list) or len(entry) != 2:
                continue
            eid, fields = entry
            d = {"id": eid}
            for i in range(0, len(fields), 2):
                d[fields[i]] = fields[i + 1]
            records.append(d)
        return records


@pytest.fixture(scope="module")
def r() -> RedisProxy:
    """Module-scoped Redis-via-docker accessor."""
    client = RedisProxy()
    assert client.ping(), "Redis not reachable inside platform container"
    return client


@pytest.fixture(scope="module", autouse=True)
def _reset_session_counters():
    """Clear per-session state in Redis before the suite runs.

    Clears:
      - session:*        — token/call/budget counters (max_tokens_per_turn)
      - trace_context:*  — stored traceparents (now NX, so stale keys would
                           prevent fresh trace propagation)
      - graph:*          — strict-graph step pointers and loop counters

    We keep the existing bearer tokens (session_token:*) so the running
    agent SDK processes don't have to re-register.
    """
    print("\n[setup] Resetting per-session state in Redis...", flush=True)
    total = 0
    for pattern in ("session:*", "trace_context:*", "graph:*"):
        out = subprocess.run(
            ["docker", "exec", PLATFORM_CONTAINER, "redis-cli", "--json",
             "KEYS", pattern],
            capture_output=True, text=True, check=False, timeout=10,
        )
        try:
            keys = json.loads(out.stdout) if out.stdout.strip() else []
        except json.JSONDecodeError:
            keys = []
        if keys:
            subprocess.run(
                ["docker", "exec", PLATFORM_CONTAINER, "redis-cli", "DEL", *keys],
                capture_output=True, text=True, check=False, timeout=10,
            )
            total += len(keys)
    print(f"[setup] Cleared {total} state keys", flush=True)


def _audit_records(r: RedisProxy, agent_id: str, since_ms: int) -> list[dict]:
    """Read audit:{agent_id} stream entries with id >= since_ms."""
    return r.xrange(f"audit:{agent_id}", min_id=f"{since_ms}-0", max_id="+")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _post_chat(message: str) -> tuple[int, dict]:
    """POST /chat to agent-sdk; return (status, body)."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(f"{AGENT_SDK}/chat", json={"message": message})
    body: dict
    try:
        body = resp.json()
    except Exception:
        body = {"_raw": resp.text}
    return resp.status_code, body


def _kinds(records: list[dict]) -> list[str]:
    return [rec.get("kind", "") for rec in records]


def _denied_records(records: list[dict]) -> list[dict]:
    return [rec for rec in records if rec.get("denied", "false") == "true"]


def _trace_ids(records: list[dict]) -> set[str]:
    return {rec.get("trace_id", "") for rec in records if rec.get("trace_id")}


def _llm_records(records: list[dict]) -> list[dict]:
    return [rec for rec in records if rec.get("kind") == "llm"]


# ── ST-S1.10 — Bitcoin (A2A + LLM) ────────────────────────────────────────────
def test_st_s1_10_bitcoin_a2a_llm(r: RedisProxy):
    """agent-sdk → LLM → A2A to agent-sdk1 → LLM → reply.

    Expectations:
      - 200 status, non-empty reply mentioning bitcoin
      - audit:agent-sdk has an LLM record (the proxy saw the LLM call)
      - A2A routing: agent-sdk has an a2a record AND agent-sdk1 has an llm
        record (cross-agent trace_id matches)
      - No denials anywhere in the flow
    """
    t0 = _now_ms()
    status, body = _post_chat("search for current bitcoin price")

    assert status == 200, f"expected 200, got {status}: {body}"
    reply = body.get("reply") or body.get("output") or body.get("message") or ""
    assert reply, f"empty reply body: {body}"
    print(f"\n  ST-S1.10 reply: {reply[:140]}…")

    time.sleep(2.0)

    sdk_recs = _audit_records(r, "agent-sdk", t0)
    sdk1_recs = _audit_records(r, "agent-sdk1", t0)

    sdk_kinds = _kinds(sdk_recs)
    sdk1_kinds = _kinds(sdk1_recs)
    print(f"  agent-sdk audit kinds  ({len(sdk_recs)}): {sdk_kinds}")
    print(f"  agent-sdk1 audit kinds ({len(sdk1_recs)}): {sdk1_kinds}")

    assert "llm" in sdk_kinds, f"agent-sdk missing llm record: {sdk_kinds}"
    assert "a2a" in sdk_kinds, f"agent-sdk missing a2a record: {sdk_kinds}"
    assert "llm" in sdk1_kinds, f"agent-sdk1 missing llm record: {sdk1_kinds}"

    # No denials expected in the bitcoin flow
    sdk_denied = _denied_records(sdk_recs)
    sdk1_denied = _denied_records(sdk1_recs)
    assert not sdk_denied, f"unexpected denials on agent-sdk: {sdk_denied}"
    assert not sdk1_denied, f"unexpected denials on agent-sdk1: {sdk1_denied}"

    # Cross-agent trace correlation
    common = _trace_ids(sdk_recs) & _trace_ids(sdk1_recs)
    print(f"  shared trace_ids: {len(common)} ({list(common)[:2]})")
    assert common, "no trace_id shared between agent-sdk and agent-sdk1"

    # Single trace_id per conversation (post-fix invariant)
    all_traces = _trace_ids(sdk_recs) | _trace_ids(sdk1_recs)
    assert len(all_traces) == 1, \
        f"expected 1 trace_id for the whole conversation, got {len(all_traces)}: {all_traces}"

    # LLM-shape flags: there must be at least one indirect (planner) call
    # AND at least one synthesis call across the agent-sdk LLM hops.
    sdk_llm = _llm_records(sdk_recs)
    indirect_calls = [r for r in sdk_llm if r.get("indirect") == "true"]
    synthesis_calls = [r for r in sdk_llm if r.get("has_tool_calls_input") == "true"]
    print(f"  agent-sdk LLM hops: {len(sdk_llm)} "
          f"(indirect={len(indirect_calls)}, synthesis={len(synthesis_calls)})")
    assert indirect_calls, "expected ≥1 LLM record with indirect=true (planner hop)"
    assert synthesis_calls, "expected ≥1 LLM record with has_tool_calls_input=true (synthesis hop)"


# ── ST-S1.11 — Weather (A2A + LLM + MCP allow) ────────────────────────────────
def test_st_s1_11_weather_a2a_mcp_allow(r: RedisProxy):
    """LLM-driven flow that touches MCP web_search (allowed by policy).

    The LLM may answer directly on agent-sdk OR route to agent-sdk2; either is
    valid. What matters is that web_search was called *somewhere* and the
    proxy allowed it (no tool-not-allowed denial).

    Expectations:
      - 200 status, non-empty reply
      - At least one ALLOWED web_search MCP call across agent-sdk + agent-sdk2
        (matched on the input payload OR the structured tool field)
      - No tool-not-allowed denial (the call wasn't rejected)
    """
    t0 = _now_ms()
    status, body = _post_chat("search for current weather in London")

    assert status == 200, f"expected 200, got {status}: {body}"
    reply = body.get("reply") or body.get("output") or body.get("message") or ""
    assert reply, f"empty reply body: {body}"
    print(f"\n  ST-S1.11 reply: {reply[:140]}…")

    time.sleep(2.0)

    sdk_recs = _audit_records(r, "agent-sdk", t0)
    sdk2_recs = _audit_records(r, "agent-sdk2", t0)
    all_recs = sdk_recs + sdk2_recs

    print(f"  agent-sdk audit kinds  ({len(sdk_recs)}): {_kinds(sdk_recs)}")
    print(f"  agent-sdk2 audit kinds ({len(sdk2_recs)}): {_kinds(sdk2_recs)}")

    # Find an ALLOWED web_search call anywhere
    web_search_calls = [
        rec for rec in all_recs
        if rec.get("denied", "false") == "false"
        and (rec.get("tool") == "web_search"
             or '"name":"web_search"' in rec.get("input", ""))
        and rec.get("kind") == "mcp"
        and "tools/call" in rec.get("input", "")
    ]
    print(f"  allowed web_search calls: {len(web_search_calls)} "
          f"(on: {[r.get('agent_id') for r in web_search_calls]})")
    assert web_search_calls, "no allowed web_search MCP call found"

    # Sanity: no tool-not-allowed denials in this flow
    bad_denials = [
        rec for rec in all_recs
        if rec.get("denied", "false") == "true"
        and "tool-not-allowed" in rec.get("denial_reason", "")
    ]
    assert not bad_denials, \
        f"unexpected tool-not-allowed denial during weather flow: {bad_denials}"


# ── ST-S1.12 — NY news (MCP tool deny) ────────────────────────────────────────
def test_st_s1_12_ny_news_mcp_deny(r: RedisProxy):
    """The agent's LLM attempts demo-mcp/dummy_email → proxy DENIES.

    The denial may happen on either agent-sdk OR agent-sdk2 depending on which
    one's LLM picks dummy_email — both are valid given the prompt. What matters
    is: somewhere in the audit log there is a `denied=true` `dummy_email` record
    with `tool-not-allowed`, and the caller does NOT receive a successful email.

    Expectations:
      - At least one denied dummy_email record across agent-sdk + agent-sdk2
      - denial_reason mentions `tool-not-allowed`
      - No allowed dummy_email record (the deny was effective — upstream not hit)
      - Caller's reply does not contain a leaked email body (no "Subject:")
    """
    t0 = _now_ms()
    status, body = _post_chat("email me the current NEW YORK news")

    print(f"\n  ST-S1.12 status={status}, body[:140]={str(body)[:140]}")

    time.sleep(2.0)

    sdk_recs = _audit_records(r, "agent-sdk", t0)
    sdk2_recs = _audit_records(r, "agent-sdk2", t0)
    all_recs = sdk_recs + sdk2_recs
    print(f"  agent-sdk audit ({len(sdk_recs)}): {_kinds(sdk_recs)}")
    print(f"  agent-sdk2 audit ({len(sdk2_recs)}): {_kinds(sdk2_recs)}")

    # Find dummy_email denials. NOTE: when the enforcer denies before all
    # metadata is set, the audit record's structured `kind`/`tool` fields can
    # be empty — but the input payload still contains "dummy_email" and the
    # denial_reason is "tool-not-allowed". We match on either signal.
    denials = [
        rec for rec in all_recs
        if rec.get("denied", "false") == "true"
        and "tool-not-allowed" in rec.get("denial_reason", "")
        and "dummy_email" in rec.get("input", "")
    ]
    print(f"  dummy_email denials: {len(denials)}")
    for rec in denials[:3]:
        print(f"    on={rec.get('agent_id')} reason={rec.get('denial_reason')!r} kind={rec.get('kind')!r}")
    assert denials, "expected at least one denied dummy_email record (denial_reason=tool-not-allowed + input mentions dummy_email)"

    # Each denied record MUST carry a structured `alert` (CLAUDE.md §5).
    for rec in denials:
        alert_str = rec.get("alert", "")
        assert alert_str, f"denied record has empty alert field: {rec}"
        try:
            alert = json.loads(alert_str)
        except (ValueError, TypeError):
            pytest.fail(f"alert field is not valid JSON: {alert_str!r}")
        assert alert.get("type") == "tool-not-allowed", \
            f"alert.type should be tool-not-allowed; got {alert!r}"
        # The synthesize path pulls `tool`/`server` from the deny envelope
        assert alert.get("tool") == "dummy_email", f"alert missing tool=dummy_email: {alert!r}"

    # Sanity: no SUCCESSFUL dummy_email upstream call. Match on either the
    # structured tool field OR the input payload to be robust.
    allowed = [
        rec for rec in all_recs
        if rec.get("denied", "false") == "false"
        and (
            rec.get("tool") == "dummy_email"
            or '"name":"dummy_email"' in rec.get("input", "")
        )
        and rec.get("kind") == "mcp"
        and "tools/call" in rec.get("input", "")
    ]
    assert not allowed, f"unexpected ALLOWED dummy_email call: {allowed}"

    # Caller's reply must not contain a plausible email body
    body_str = json.dumps(body).lower()
    assert "subject:" not in body_str, \
        f"dummy_email content leaked through despite denial: {body}"
