"""
Sprint S7 system test — ST-UM.7 (User Management: debug-endpoint authorization).

Validates that the per-user live debugger (S6) is now also owner-gated (S7):
the PUT /agents/{id}/debug endpoint enforces auth BEFORE its missing-UUID 400,
lets the owner (or an admin, who bypasses ownership) toggle debug, and refuses
a non-owner with 403 WITHOUT ever writing the debug enabled key.

Runs full-stack against the LIVE ``amaze-platform`` container exactly like
test_s4.py / test_s6 (host ports 8001/orch, 6379/redis on 127.0.0.1). No mocks.

We exercise ``agent-sdk`` — owned by the bootstrap admin. The owner-path uses
``admin_client`` because admin bypasses ownership (so the test does not depend
on which concrete user owns the demo agent). Every debug key created is cleaned
up via try/finally.

Behaviour contract (verified live):
  * PUT /agents/{id}/debug needs the per-user header X-Amaze-Debug-User.
  * Auth is checked first: NO cookie → 401 (BEFORE the 400 missing-UUID).
  * Owner/admin + UUID → 200 and Redis debug:{id}:{uuid}:enabled == "1".
  * Non-owner + UUID → 403 and the debug enabled key is NOT created.
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

# Demo agent owned by the bootstrap admin (admin bypasses ownership checks).
DEMO_AGENT = "agent-sdk"


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


def _du(user: str) -> dict[str, str]:
    """The per-user debug header the endpoint requires."""
    return {"X-Amaze-Debug-User": user}


def _enabled_key(agent: str, debug_user: str) -> str:
    return f"debug:{agent}:{debug_user}:enabled"


# ── ST-UM.7 — Debug endpoint is auth-gated, owner-gated, and order-correct ──


def test_st_um_7_debug_requires_auth() -> None:
    """ST-UM.7a — An unauthenticated request to PUT /agents/{id}/debug with NO
    cookie and NO debug-user header → 401 (NOT 400).

    This pins the check ORDER: auth fires before the missing-UUID 400, so an
    anonymous caller can never learn whether the UUID would have been valid.
    """
    with httpx.Client(base_url=ORCH, timeout=15.0) as anon:
        r = anon.put(f"/agents/{DEMO_AGENT}/debug", json={"enabled": True})
        assert r.status_code == 401, (
            f"unauth debug toggle must 401 (before 400): {r.status_code} {r.text}"
        )


def test_st_um_7_owner_can_debug(
    s7_seed, admin_client: httpx.Client, rc: redis.Redis
) -> None:
    """ST-UM.7b — admin (bypasses ownership) toggles debug on with a UUID → 200
    and the per-user enabled key is written.

    Asserts:
      * PUT /agents/agent-sdk/debug {enabled:true} + X-Amaze-Debug-User → 200.
      * Redis debug:agent-sdk:{uuid}:enabled == "1".
    CLEANUP: disable ({enabled:false}) and delete the key.
    """
    debug_user = f"s7-uuid-{uuid.uuid4().hex}"
    key = _enabled_key(DEMO_AGENT, debug_user)
    try:
        r = admin_client.put(
            f"/agents/{DEMO_AGENT}/debug",
            json={"enabled": True},
            headers=_du(debug_user),
        )
        assert r.status_code == 200, (
            f"owner/admin debug enable must 200: {r.status_code} {r.text}"
        )
        assert rc.get(key) == "1", (
            f"{key} must == '1' after owner enabled debug; got {rc.get(key)!r}"
        )
    finally:
        # Best-effort: release the dead-man's-switch, then purge the key.
        try:
            admin_client.put(
                f"/agents/{DEMO_AGENT}/debug",
                json={"enabled": False},
                headers=_du(debug_user),
            )
        except httpx.HTTPError:
            pass
        rc.delete(key)


def test_st_um_7_non_owner_blocked(
    s7_seed, user_b_client: httpx.Client, rc: redis.Redis
) -> None:
    """ST-UM.7c — A non-owner toggling debug with a valid UUID → 403, and the
    debug enabled key is NEVER written (authz fires before any debug write).

    Asserts:
      * user-b PUT /agents/agent-sdk/debug {enabled:true} + X-Amaze-Debug-User → 403.
      * Redis debug:agent-sdk:{uuid}:enabled does NOT exist.
    """
    debug_user = f"s7-uuid2-{uuid.uuid4().hex}"
    key = _enabled_key(DEMO_AGENT, debug_user)
    try:
        r = user_b_client.put(
            f"/agents/{DEMO_AGENT}/debug",
            json={"enabled": True},
            headers=_du(debug_user),
        )
        assert r.status_code == 403, (
            f"non-owner debug enable must 403: {r.status_code} {r.text}"
        )
        assert rc.get(key) is None, (
            f"{key} must NOT be created when a non-owner is denied; "
            f"got {rc.get(key)!r}"
        )
    finally:
        rc.delete(key)
