"""
Sprint S7 system tests — ST-UM.4, .5, .6 (User Management: ownership model).

Validates the claim → register → own lifecycle and the owner-gating it enables:

  * A logged-in user can CLAIM an unowned, unregistered agent id (NX reserve).
  * The OPEN /register endpoint binds ownership to the claimer if a claim exists.
  * Owner-gated routes (policy / messages / stats / ...) reject non-owners 403.
  * /register with NO prior claim quarantines the agent (owner stays unset) and
    a second user cannot claim an already-registered id (409).

These run full-stack against the LIVE ``amaze-platform`` container exactly like
test_s4.py / test_s6 (host ports 8001/orch, 6379/redis on 127.0.0.1). No mocks.

Each test uses unique random agent ids (``t-own-<hex>`` / ``t-squat-<hex>``) so
concurrent reruns never collide, and CLEANS UP every key it creates via
try/finally — owner/claim/endpoint/chat_endpoint/approved, agent_session,
debug:*, and the per-user reverse index SET.

Ownership keys (services/orchestrator/auth.py):
  agent:{id}:owner    STRING → user_id
  user:{uid}:agents   SET    {agent_id, ...}
  agent:{id}:claim    STRING → user_id   (NX pre-registration reserve)
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest
import redis

# ── connection settings (mirror test_s6 — live stack) ──────────────────────

ORCH = os.environ.get("AMAZE_ORCHESTRATOR_S7", "http://localhost:8001")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def rc() -> redis.Redis:
    """Live Redis client — same skip-on-unreachable contract as test_s6."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
    except redis.RedisError as e:
        pytest.skip(f"redis unreachable at {REDIS_HOST}:{REDIS_PORT}: {e}")
    yield r
    r.close()


# ── helpers ────────────────────────────────────────────────────────────────


def _user_id(rc: redis.Redis, username: str) -> str:
    uid = rc.get(f"username:{username}")
    assert uid, f"expected username:{username} to resolve to a user_id in Redis"
    return uid


def _cleanup_agent(rc: redis.Redis, agent_id: str) -> None:
    """Delete every key an agent's claim/register lifecycle could have created,
    and remove it from any user's reverse index. Safe to call unconditionally."""
    # Drop the agent from whatever owner's reverse-index SET it landed in.
    owner = rc.get(f"agent:{agent_id}:owner")
    claimer = rc.get(f"agent:{agent_id}:claim")
    for uid in {owner, claimer}:
        if uid:
            rc.srem(f"user:{uid}:agents", agent_id)
    rc.delete(
        f"agent:{agent_id}:owner",
        f"agent:{agent_id}:claim",
        f"agent:{agent_id}:endpoint",
        f"agent:{agent_id}:chat_endpoint",
        f"agent:{agent_id}:approved",
        f"agent_session:{agent_id}",
        f"policy:{agent_id}",
    )
    # Any per-user debug keys for this agent.
    for key in rc.scan_iter(match=f"debug:{agent_id}:*"):
        rc.delete(key)


def _rand_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── ST-UM.4 — Claim then register binds ownership ──────────────────────────


def test_st_um_4_claim_then_register_binds(
    s7_seed, user_a_client: httpx.Client, rc: redis.Redis
) -> None:
    """ST-UM.4 — user-a claims an id, then the OPEN /register binds ownership.

    Asserts:
      * POST /agents/{id}/claim as user-a → 200 and agent:{id}:claim == a-uid.
      * POST /register {agent_id,a2a_host,a2a_port} → 2xx; afterwards
        agent:{id}:owner == a-uid AND {id} ∈ user:{a-uid}:agents.
    """
    a_uid = _user_id(rc, "s7-user-a")
    agent_id = _rand_id("t-own")
    try:
        claim = user_a_client.post(f"/agents/{agent_id}/claim")
        assert claim.status_code == 200, f"claim must 200: {claim.status_code} {claim.text}"
        assert rc.get(f"agent:{agent_id}:claim") == a_uid, (
            f"agent:{agent_id}:claim must == user-a uid {a_uid}"
        )

        # /register is OPEN (no auth) — a claim already reserves the owner.
        with httpx.Client(base_url=ORCH, timeout=15.0) as anon:
            reg = anon.post(
                "/register",
                json={
                    "agent_id": agent_id,
                    "a2a_host": "127.0.0.1",
                    "a2a_port": 9999,
                },
            )
        assert reg.status_code in (200, 201), (
            f"register must succeed: {reg.status_code} {reg.text}"
        )

        # The claim is now bound to ownership + the reverse index.
        assert rc.get(f"agent:{agent_id}:owner") == a_uid, (
            f"agent:{agent_id}:owner must == user-a uid {a_uid} after register"
        )
        assert rc.sismember(f"user:{a_uid}:agents", agent_id), (
            f"{agent_id} must be in user:{a_uid}:agents reverse index"
        )
    finally:
        _cleanup_agent(rc, agent_id)


# ── ST-UM.5 — Cross-user owner gating ──────────────────────────────────────


def test_st_um_5_cross_user_denied(
    s7_seed,
    user_a_client: httpx.Client,
    user_b_client: httpx.Client,
    rc: redis.Redis,
) -> None:
    """ST-UM.5 — Once user-a owns an agent, user-b is locked out of its owner
    routes; the real owner can write its policy.

    Asserts (agent owned by user-a):
      * user-b PUT /policy/{id}            → 403.
      * user-b POST /agents/{id}/messages  → 403.
      * user-b GET  /agents/{id}/stats     → 403.
      * user-a PUT /policy/{id} (minimal valid Policy) → 200 and policy:{id}
        exists in Redis.
    """
    a_uid = _user_id(rc, "s7-user-a")
    agent_id = _rand_id("t-own")
    try:
        # user-a takes ownership via claim → register.
        claim = user_a_client.post(f"/agents/{agent_id}/claim")
        assert claim.status_code == 200, f"claim must 200: {claim.status_code} {claim.text}"
        with httpx.Client(base_url=ORCH, timeout=15.0) as anon:
            reg = anon.post(
                "/register",
                json={"agent_id": agent_id, "a2a_host": "127.0.0.1", "a2a_port": 9999},
            )
        assert reg.status_code in (200, 201), f"register failed: {reg.status_code} {reg.text}"
        assert rc.get(f"agent:{agent_id}:owner") == a_uid, "user-a must own the agent"

        # Non-owner (user-b) is forbidden on every owner-gated route.
        r_pol = user_b_client.put(
            f"/policy/{agent_id}", json={"name": agent_id, "mode": "flexible"}
        )
        assert r_pol.status_code == 403, (
            f"non-owner PUT /policy must 403: {r_pol.status_code} {r_pol.text}"
        )

        r_msg = user_b_client.post(
            f"/agents/{agent_id}/messages", json={"prompt": "hello"}
        )
        assert r_msg.status_code == 403, (
            f"non-owner POST /messages must 403: {r_msg.status_code} {r_msg.text}"
        )

        r_stats = user_b_client.get(f"/agents/{agent_id}/stats")
        assert r_stats.status_code == 403, (
            f"non-owner GET /stats must 403: {r_stats.status_code} {r_stats.text}"
        )

        # The real owner can write a minimal valid policy.
        owner_put = user_a_client.put(
            f"/policy/{agent_id}", json={"name": agent_id, "mode": "flexible"}
        )
        assert owner_put.status_code == 200, (
            f"owner PUT /policy must 200: {owner_put.status_code} {owner_put.text}"
        )
        assert rc.exists(f"policy:{agent_id}"), (
            f"policy:{agent_id} must exist in Redis after owner write"
        )
    finally:
        _cleanup_agent(rc, agent_id)


# ── ST-UM.6 — Quarantine on unclaimed register, no id grab ──────────────────


def test_st_um_6_quarantine_and_no_grab(
    s7_seed,
    user_a_client: httpx.Client,
    user_b_client: httpx.Client,
    rc: redis.Redis,
) -> None:
    """ST-UM.6 — Registering with NO prior claim quarantines the agent (no
    owner), and a later claim on an already-registered id is rejected.

    Asserts:
      * POST /register {id} with NO prior claim → agent:{id}:owner is None.
      * user-a GET /agents does NOT include the quarantined id (admin sees all,
        so we check against a non-admin instead).
      * user-b POST /agents/{id}/claim (already registered) → 409.
    """
    agent_id = _rand_id("t-squat")
    try:
        # Register straight away — nobody claimed it first.
        with httpx.Client(base_url=ORCH, timeout=15.0) as anon:
            reg = anon.post(
                "/register",
                json={"agent_id": agent_id, "a2a_host": "127.0.0.1", "a2a_port": 9999},
            )
        assert reg.status_code in (200, 201), f"register failed: {reg.status_code} {reg.text}"

        # Quarantined: no owner bound.
        assert rc.get(f"agent:{agent_id}:owner") is None, (
            f"unclaimed register must NOT bind an owner: "
            f"agent:{agent_id}:owner={rc.get(f'agent:{agent_id}:owner')!r}"
        )

        # A non-admin user must not see the quarantined agent in its list.
        lst = user_a_client.get("/agents")
        assert lst.status_code == 200, f"/agents must 200: {lst.status_code} {lst.text}"
        ids: set[str] = set()
        for item in lst.json():
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict):
                ids.add(item.get("agent_id") or item.get("id") or item.get("name"))
        assert agent_id not in ids, (
            f"quarantined {agent_id} must not appear in user-a's list: {sorted(ids)}"
        )

        # The id is already registered → a fresh claim must be refused with 409.
        claim = user_b_client.post(f"/agents/{agent_id}/claim")
        assert claim.status_code == 409, (
            f"claim on a registered id must 409: {claim.status_code} {claim.text}"
        )
    finally:
        _cleanup_agent(rc, agent_id)


# ── Delete cleans the owner's reverse index (review fix #1) ─────────────────


def test_delete_cleans_reverse_index(
    s7_seed, user_a_client: httpx.Client, rc: redis.Redis
) -> None:
    """DELETE /agents/{id} must drop the id from the owner's reverse-index SET.

    Regression test for the bug where the `agent:{id}:*` scan-delete wiped the
    owner key *before* the reverse-index cleanup read it, leaving a stale
    phantom entry in `user:{owner}:agents` forever.
    """
    a_uid = _user_id(rc, "s7-user-a")
    agent_id = _rand_id("t-del")
    try:
        assert user_a_client.post(f"/agents/{agent_id}/claim").status_code == 200
        with httpx.Client(base_url=ORCH, timeout=15.0) as anon:
            reg = anon.post(
                "/register",
                json={"agent_id": agent_id, "a2a_host": "127.0.0.1", "a2a_port": 9999},
            )
        assert reg.status_code in (200, 201), f"register failed: {reg.text}"
        assert rc.sismember(f"user:{a_uid}:agents", agent_id), "precondition: owned"

        d = user_a_client.delete(f"/agents/{agent_id}")
        assert d.status_code == 200, f"owner delete must 200: {d.status_code} {d.text}"

        assert rc.get(f"agent:{agent_id}:owner") is None, "owner key must be gone"
        assert not rc.sismember(f"user:{a_uid}:agents", agent_id), (
            f"{agent_id} must be removed from user:{a_uid}:agents after delete "
            "(stale reverse-index entry = the bug this guards)"
        )
    finally:
        _cleanup_agent(rc, agent_id)
