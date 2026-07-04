"""
Sprint S8 system tests — admin user management + agent (re)assignment.

Full-stack against the LIVE ``amaze-platform`` container (orch :8001, redis
:6379 on 127.0.0.1), same convention as test_s7_*. No mocks.

Covers the S8 admin console seam:
  * ST-UM.9  — POST /auth/users is admin-only (201 for admin, 403 for a
               regular user); GET /auth/users lists; duplicate → 409.
  * ST-UM.10 — POST /agents/{id}/assign reassigns to a single owner: the new
               owner's reverse index gains the id, the OLD owner's loses it
               (one-agent-one-owner). Non-admin → 403.
  * ST-UM.11 — DELETE /auth/users/{id} orphans the user's agents (owner key
               cleared, agent stays registered); admin cannot delete self (400).

Fixtures (tests/conftest.py): admin_client (s7-root, admin), user_a_client /
user_b_client (regular). Every test cleans up the keys it creates.
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
    assert uid, f"expected username:{username} → user_id in Redis"
    return uid


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _cleanup_user(rc: redis.Redis, username: str) -> None:
    uid = rc.get(f"username:{username}")
    if uid:
        rc.delete(f"user:{uid}", f"user:{uid}:cred", f"user:{uid}:agents")
    rc.delete(f"username:{username}")


def _cleanup_agent(rc: redis.Redis, agent_id: str) -> None:
    for uid in {rc.get(f"agent:{agent_id}:owner"), rc.get(f"agent:{agent_id}:claim")}:
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


def _register(agent_id: str) -> None:
    with httpx.Client(base_url=ORCH, timeout=15.0) as anon:
        reg = anon.post(
            "/register",
            json={"agent_id": agent_id, "a2a_host": "127.0.0.1", "a2a_port": 9999},
        )
    assert reg.status_code in (200, 201), f"register failed: {reg.status_code} {reg.text}"


# ── ST-UM.9 — admin-only user creation ──────────────────────────────────────


def test_st_um_9_create_user_admin_only(
    s7_seed, admin_client: httpx.Client, user_a_client: httpx.Client, rc: redis.Redis
) -> None:
    uname = _rand("t-newuser")
    try:
        # Regular user is forbidden.
        denied = user_a_client.post(
            "/auth/users", json={"username": uname, "password": "pw", "role": "user"}
        )
        assert denied.status_code == 403, f"non-admin create must 403: {denied.text}"
        assert rc.get(f"username:{uname}") is None, "no user must exist after 403"

        # Admin creates a regular user.
        ok = admin_client.post(
            "/auth/users", json={"username": uname, "password": "pw", "role": "user"}
        )
        assert ok.status_code == 201, f"admin create must 201: {ok.status_code} {ok.text}"
        body = ok.json()
        assert body["username"] == uname and body["role"] == "user"
        assert rc.hget(f"user:{body['user_id']}", "role") == "user"

        # Listing (admin-only) includes the new user; a regular user is 403.
        assert user_a_client.get("/auth/users").status_code == 403
        listed = admin_client.get("/auth/users")
        assert listed.status_code == 200
        assert any(u["username"] == uname for u in listed.json())

        # Duplicate username → 409.
        dup = admin_client.post(
            "/auth/users", json={"username": uname, "password": "pw", "role": "admin"}
        )
        assert dup.status_code == 409, f"duplicate must 409: {dup.status_code} {dup.text}"
    finally:
        _cleanup_user(rc, uname)


# ── ST-UM.10 — single-owner reassignment ────────────────────────────────────


def test_st_um_10_assign_moves_single_owner(
    s7_seed,
    admin_client: httpx.Client,
    user_a_client: httpx.Client,
    user_b_client: httpx.Client,
    rc: redis.Redis,
) -> None:
    a_uid = _user_id(rc, "s7-user-a")
    b_uid = _user_id(rc, "s7-user-b")
    agent_id = _rand("t-assign")
    try:
        # user-a owns it via claim → register.
        assert user_a_client.post(f"/agents/{agent_id}/claim").status_code == 200
        _register(agent_id)
        assert rc.get(f"agent:{agent_id}:owner") == a_uid
        assert rc.sismember(f"user:{a_uid}:agents", agent_id)

        # A non-admin cannot reassign.
        denied = user_a_client.post(f"/agents/{agent_id}/assign", json={"user_id": b_uid})
        assert denied.status_code == 403, f"non-admin assign must 403: {denied.text}"

        # Admin reassigns a → b. Single owner: a's index loses it, b's gains it.
        ok = admin_client.post(f"/agents/{agent_id}/assign", json={"user_id": b_uid})
        assert ok.status_code == 200, f"admin assign must 200: {ok.status_code} {ok.text}"
        assert rc.get(f"agent:{agent_id}:owner") == b_uid
        assert rc.sismember(f"user:{b_uid}:agents", agent_id), "new owner index must gain id"
        assert not rc.sismember(f"user:{a_uid}:agents", agent_id), (
            "OLD owner index must lose id — one agent, one owner"
        )

        # Assigning to a non-existent user → 404, owner unchanged.
        bad = admin_client.post(
            f"/agents/{agent_id}/assign", json={"user_id": "nope-" + uuid.uuid4().hex}
        )
        assert bad.status_code == 404, f"assign to unknown user must 404: {bad.text}"
        assert rc.get(f"agent:{agent_id}:owner") == b_uid
    finally:
        _cleanup_agent(rc, agent_id)


# ── ST-UM.11 — delete orphans agents; no self-delete ────────────────────────


def test_st_um_11_delete_user_orphans_and_no_self(
    s7_seed, admin_client: httpx.Client, rc: redis.Redis
) -> None:
    uname = _rand("t-deluser")
    agent_id = _rand("t-orphan")
    try:
        created = admin_client.post(
            "/auth/users", json={"username": uname, "password": "pw", "role": "user"}
        )
        assert created.status_code == 201
        uid = created.json()["user_id"]

        # Give the user an agent, then delete the user.
        _register(agent_id)
        assert admin_client.post(
            f"/agents/{agent_id}/assign", json={"user_id": uid}
        ).status_code == 200
        assert rc.get(f"agent:{agent_id}:owner") == uid

        d = admin_client.delete(f"/auth/users/{uid}")
        assert d.status_code == 204, f"delete user must 204: {d.status_code} {d.text}"
        assert rc.get(f"username:{uname}") is None, "username mapping must be gone"
        assert not rc.exists(f"user:{uid}"), "user hash must be gone"
        # Agent is orphaned, not deleted: owner cleared but still registered.
        assert rc.get(f"agent:{agent_id}:owner") is None, "agent must be orphaned"
        assert rc.exists(f"agent:{agent_id}:endpoint"), "agent must stay registered"

        # Admin cannot delete itself.
        me = admin_client.get("/auth/me").json()
        self_del = admin_client.delete(f"/auth/users/{me['user_id']}")
        assert self_del.status_code == 400, f"self-delete must 400: {self_del.text}"
    finally:
        _cleanup_user(rc, uname)
        _cleanup_agent(rc, agent_id)
