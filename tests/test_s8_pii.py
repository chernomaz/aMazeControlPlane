"""
Sprint S8 system tests — PII redaction endpoints (live-stack).

Runs against the LIVE ``amaze-platform`` stack (orchestrator :8001, Redis
:6379), same convention as tests/test_s7_*.py. The whole module skips if the
stack is not up.

Covers the config-level slice of ST-PII.*:

  ST-PII.1  PUT then GET round-trip on `pii_config` sub-object
  ST-PII.2  Cross-user 403 on GET / PUT / preview
  ST-PII.6  No config = pass-through (endpoint returns empty PiiConfig())
  ST-PII.7  Preview endpoint returns redacted values, does not persist
  ST-PII.8  Validation: unknown entity → 422 on PUT and preview
  ST-PII.extra  PUT 404 when the underlying policy is absent

The traffic-level slice (ST-PII.3/4/5 input & POST-SSE & GET-SSE redaction
end-to-end via an agent + mock MCP) is left for a follow-up harness because
it requires driving the mock_mcp scaffold with a scripted tool call and
asserting on the audit stream — the plumbing here is covered by the unit
tests in test_s8_pii_engine.py plus the /preview endpoint below.

Fixtures used from conftest.py (S7 seed): admin_client, user_a_client,
user_b_client.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
import redis


ORCH = os.environ.get("AMAZE_ORCHESTRATOR_S7", "http://localhost:8001")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))


@pytest.fixture
def rc() -> redis.Redis:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
    except redis.RedisError as e:
        pytest.skip(f"redis unreachable at {REDIS_HOST}:{REDIS_PORT}: {e}")
    yield r
    r.close()


def _user_id(rc: redis.Redis, username: str) -> str:
    uid = rc.get(f"username:{username}")
    assert uid, f"username:{username} missing"
    return uid


def _cleanup_agent(rc: redis.Redis, agent_id: str, *usernames: str) -> None:
    """Drop all keys that PUT /policy or the claim path might have created."""
    for username in usernames:
        uid = rc.get(f"username:{username}")
        if uid:
            rc.srem(f"user:{uid}:agents", agent_id)
    rc.delete(
        f"agent:{agent_id}:owner",
        f"agent:{agent_id}:claim",
        f"policy:{agent_id}",
    )


def _seed_policy_owned_by(
    rc: redis.Redis,
    agent_id: str,
    owner_username: str,
) -> None:
    """Give `owner_username` a policy that the PII tests can layer on top of.

    Ownership + minimal policy are written directly to Redis (mirrors the
    poke-Redis pattern in tests/test_s7_ownership.py) so the tests don't need
    a proxy hop.
    """
    import json
    owner_uid = _user_id(rc, owner_username)
    rc.set(f"agent:{agent_id}:claim", owner_uid)
    rc.set(f"agent:{agent_id}:owner", owner_uid)
    rc.sadd(f"user:{owner_uid}:agents", agent_id)
    rc.set(f"policy:{agent_id}", json.dumps({
        "name": agent_id,
        "max_tokens_per_turn": 0,
        "max_tool_calls_per_turn": 0,
        "max_agent_calls_per_turn": 0,
        "allowed_llm_providers": [],
        "token_rate_limits": [],
        "on_budget_exceeded": "block",
        "on_violation": "block",
        "mode": "flexible",
        "allowed_tools": ["web_search"],
        "allowed_agents": [],
        "graph": None,
    }))


# ---------------------------------------------------------------------------
# ST-PII.1 — round-trip
# ---------------------------------------------------------------------------

def test_stpii1_put_then_get_round_trip(rc, user_a_client: httpx.Client) -> None:
    """PUT /policy/{id}/pii writes; GET returns identical structure; the
    pii_config sub-object is stored inside policy:{id}."""
    agent = f"t-pii-{uuid.uuid4().hex[:8]}"
    try:
        _seed_policy_owned_by(rc=rc, agent_id=agent, owner_username="s7-user-a")

        payload = {
            "enabled": True,
            "tools": {"web_search": {
                "input": {"query": {"entities": ["EMAIL_ADDRESS", "PERSON"]}},
                "output": {"entities": ["PHONE_NUMBER"]},
            }},
        }
        r = user_a_client.put(f"{ORCH}/policy/{agent}/pii", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"updated": True, "agent_id": agent, "tools": 1}

        r = user_a_client.get(f"{ORCH}/policy/{agent}/pii")
        assert r.status_code == 200
        got = r.json()
        assert got["enabled"] is True
        assert got["tools"]["web_search"]["input"]["query"]["entities"] == \
            ["EMAIL_ADDRESS", "PERSON"]
        assert got["tools"]["web_search"]["output"]["entities"] == ["PHONE_NUMBER"]

        # The pii_config lives INSIDE policy:{id}
        import json
        policy_raw = rc.get(f"policy:{agent}")
        assert policy_raw
        policy = json.loads(policy_raw)
        assert policy["pii_config"]["tools"]["web_search"]["output"]["entities"] == ["PHONE_NUMBER"]
    finally:
        _cleanup_agent(rc, agent, "s7-user-a")


# ---------------------------------------------------------------------------
# ST-PII.2 — cross-user denial
# ---------------------------------------------------------------------------

def test_stpii2_cross_user_denied(rc, user_a_client: httpx.Client,
                                    user_b_client: httpx.Client) -> None:
    """User B cannot GET, PUT, or preview PII config for user A's agent."""
    agent = f"t-pii-{uuid.uuid4().hex[:8]}"
    try:
        _seed_policy_owned_by(rc=rc, agent_id=agent, owner_username="s7-user-a")

        # A can PUT; B cannot see or write
        r = user_a_client.put(f"{ORCH}/policy/{agent}/pii", json={"enabled": True, "tools": {}})
        assert r.status_code == 200

        r = user_b_client.get(f"{ORCH}/policy/{agent}/pii")
        assert r.status_code == 403, r.text
        r = user_b_client.put(f"{ORCH}/policy/{agent}/pii",
                              json={"enabled": True, "tools": {}})
        assert r.status_code == 403
        r = user_b_client.post(f"{ORCH}/policy/{agent}/pii/preview",
                               json={"tool": "web_search", "input": {},
                                     "input_entities": {}, "response_text": None,
                                     "output_entities": []})
        assert r.status_code == 403

        # Redis policy untouched by B's attempts.
        import json
        policy = json.loads(rc.get(f"policy:{agent}") or "{}")
        assert policy.get("pii_config", {}).get("tools", {}) == {}
    finally:
        _cleanup_agent(rc, agent, "s7-user-a", "s7-user-b")


# ---------------------------------------------------------------------------
# ST-PII.6 — no config = pass-through (endpoint returns empty PiiConfig)
# ---------------------------------------------------------------------------

def test_stpii6_get_returns_empty_config_when_pii_absent(
    rc, user_a_client: httpx.Client,
) -> None:
    agent = f"t-pii-{uuid.uuid4().hex[:8]}"
    try:
        _seed_policy_owned_by(rc=rc, agent_id=agent, owner_username="s7-user-a")
        r = user_a_client.get(f"{ORCH}/policy/{agent}/pii")
        assert r.status_code == 200, r.text
        assert r.json() == {"enabled": True, "tools": {}}
    finally:
        _cleanup_agent(rc, agent, "s7-user-a")


# ---------------------------------------------------------------------------
# ST-PII.7 — preview does not persist, returns redacted result
# ---------------------------------------------------------------------------

def test_stpii7_preview_redacts_without_persisting(
    rc, user_a_client: httpx.Client,
) -> None:
    agent = f"t-pii-{uuid.uuid4().hex[:8]}"
    try:
        _seed_policy_owned_by(rc=rc, agent_id=agent, owner_username="s7-user-a")

        r = user_a_client.post(f"{ORCH}/policy/{agent}/pii/preview", json={
            "tool": "web_search",
            "input": {"query": "email me at john@x.com about Jane Smith"},
            "input_entities": {"query": ["EMAIL_ADDRESS", "PERSON"]},
            "response_text": "Call 415-555-2019 or ops@acme.com",
            "output_entities": ["PHONE_NUMBER", "EMAIL_ADDRESS"],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["input"]["query"] == "email me at <EMAIL_ADDRESS> about <PERSON>"
        assert body["response_text"] == "Call <PHONE_NUMBER> or <EMAIL_ADDRESS>"

        # Nothing persisted.
        import json
        policy = json.loads(rc.get(f"policy:{agent}") or "{}")
        assert policy.get("pii_config") in (None, {"enabled": True, "tools": {}})
    finally:
        _cleanup_agent(rc, agent, "s7-user-a")


# ---------------------------------------------------------------------------
# ST-PII.8 — validation
# ---------------------------------------------------------------------------

def test_stpii8_unknown_entity_rejected(rc, user_a_client: httpx.Client) -> None:
    agent = f"t-pii-{uuid.uuid4().hex[:8]}"
    try:
        _seed_policy_owned_by(rc=rc, agent_id=agent, owner_username="s7-user-a")

        r = user_a_client.put(f"{ORCH}/policy/{agent}/pii", json={
            "enabled": True,
            "tools": {"web_search": {
                "input": {"query": {"entities": ["NOT_A_REAL_ENTITY"]}},
            }},
        })
        assert r.status_code == 422, r.text

        r = user_a_client.post(f"{ORCH}/policy/{agent}/pii/preview", json={
            "tool": "web_search", "input": {}, "input_entities": {},
            "response_text": None, "output_entities": ["ALSO_FAKE"],
        })
        assert r.status_code == 422, r.text
    finally:
        _cleanup_agent(rc, agent, "s7-user-a")


# ---------------------------------------------------------------------------
# Extra — PUT 404 when the policy row is absent (agent id not seeded)
# ---------------------------------------------------------------------------

def test_stpii_put_404_when_policy_absent(rc, user_a_client: httpx.Client) -> None:
    agent = f"t-nopol-{uuid.uuid4().hex[:8]}"
    try:
        # Reserve ownership so `require_agent_owner` does not 403 first —
        # we want to see 404 from the endpoint's own policy-missing branch.
        uid = _user_id(rc, "s7-user-a")
        rc.set(f"agent:{agent}:owner", uid)
        rc.sadd(f"user:{uid}:agents", agent)

        r = user_a_client.put(f"{ORCH}/policy/{agent}/pii",
                              json={"enabled": True, "tools": {}})
        assert r.status_code == 404, r.text
    finally:
        _cleanup_agent(rc, agent, "s7-user-a")
