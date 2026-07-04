"""
Sprint S7 system tests — ST-UM.1, .2, .3 (User Management: auth + login session).

Validates the human-identity layer added in S7: password login mints an opaque
session cookie backed by ``user_session:{token}`` in Redis; gated routes 401
without it; logout revokes the token; and ``GET /agents`` is owner-filtered
(admins see all, a fresh user sees only what it owns).

These run full-stack against the LIVE ``amaze-platform`` container exactly like
test_s4.py / test_s6 (host ports 8001/orch, 6379/redis on 127.0.0.1). We do NOT
spin up tests/compose.test.yml — the live demo stack is the device under test.
Real orchestrator, real Redis — no mocks.

Override hosts:
  AMAZE_ORCHESTRATOR_S7  http://localhost:8001  (set in conftest as S7_ORCH)
  REDIS_HOST             127.0.0.1
  REDIS_PORT             6379

Fixtures (from conftest.py):
  s7_seed         — seeds s7-admin (admin), s7-user-a, s7-user-b; skips if down.
  admin_client    — httpx.Client logged in as s7-admin (cookie in jar).
  user_a_client   — httpx.Client logged in as s7-user-a.
  user_b_client   — httpx.Client logged in as s7-user-b.

Behaviour contract (verified live):
  * POST /auth/login {username,password} → 200 + Set-Cookie amaze_session;
    bad creds → 401. Redis user_session:{cookie} → user_id.
  * POST /auth/logout → 204; clears the session key; later gated calls → 401.
  * GET /agents → 401 without cookie; admin sees all demo agents; a fresh
    user that owns nothing → [].
"""
from __future__ import annotations

import os

import httpx
import pytest
import redis

# ── connection settings (mirror test_s6 — live stack) ──────────────────────

ORCH = os.environ.get("AMAZE_ORCHESTRATOR_S7", "http://localhost:8001")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

SESSION_COOKIE = "amaze_session"

# A demo agent owned by the bootstrap admin — admin sees it, a fresh user does not.
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


def _user_id(rc: redis.Redis, username: str) -> str:
    uid = rc.get(f"username:{username}")
    assert uid, f"expected username:{username} to resolve to a user_id in Redis"
    return uid


def _cookie_token(client: httpx.Client) -> str:
    """Pull the amaze_session cookie value out of a logged-in client's jar."""
    token = client.cookies.get(SESSION_COOKIE)
    assert token, f"client has no {SESSION_COOKIE} cookie: {dict(client.cookies)}"
    return token


# ── ST-UM.1 — Login mints a Redis-backed session ───────────────────────────


def test_st_um_1_login_session(s7_seed, rc: redis.Redis) -> None:
    """ST-UM.1 — Bad password is rejected; a good login mints a cookie whose
    token resolves to the admin user_id in Redis.

    Asserts:
      * POST /auth/login with a wrong password → 401.
      * POST /auth/login with correct admin creds → 200 + amaze_session cookie.
      * Redis user_session:{cookie} == the admin user_id (username:s7-admin).
    """
    admin_uid = _user_id(rc, "s7-admin")

    with httpx.Client(base_url=ORCH, timeout=15.0) as c:
        bad = c.post(
            "/auth/login",
            json={"username": "s7-admin", "password": "wrong-password"},
        )
        assert bad.status_code == 401, f"bad password must 401: {bad.status_code} {bad.text}"
        # A failed login must not set a session cookie.
        assert SESSION_COOKIE not in c.cookies, "failed login must not set a cookie"

        good = c.post(
            "/auth/login",
            json={"username": "s7-admin", "password": "s7-admin-pass"},
        )
        assert good.status_code == 200, f"good login must 200: {good.status_code} {good.text}"

        token = _cookie_token(c)
        # The cookie token is the Redis session key — it must resolve to admin.
        assert rc.get(f"user_session:{token}") == admin_uid, (
            f"user_session:{token} must resolve to admin uid {admin_uid}"
        )


# ── ST-UM.2 — Auth gate + logout revocation ────────────────────────────────


def test_st_um_2_auth_required_and_logout(
    s7_seed, admin_client: httpx.Client, rc: redis.Redis
) -> None:
    """ST-UM.2 — GET /agents requires a session; logout revokes it.

    Asserts:
      * An UNauthenticated client → GET /agents → 401.
      * admin_client (logged in) → GET /agents → 200.
      * POST /auth/logout → 204 and the just-used user_session:{token} key is gone.
      * A follow-up GET /agents on a fresh unauth client → 401.
    """
    # Unauthenticated: gated route must 401.
    with httpx.Client(base_url=ORCH, timeout=15.0) as anon:
        r = anon.get("/agents")
        assert r.status_code == 401, f"unauth /agents must 401: {r.status_code} {r.text}"

    # Authenticated admin: 200.
    ok = admin_client.get("/agents")
    assert ok.status_code == 200, f"admin /agents must 200: {ok.status_code} {ok.text}"

    # Capture the live session token, then log out.
    token = _cookie_token(admin_client)
    assert rc.get(f"user_session:{token}"), "session key must exist before logout"

    out = admin_client.post("/auth/logout")
    assert out.status_code == 204, f"logout must 204: {out.status_code} {out.text}"
    assert rc.get(f"user_session:{token}") is None, (
        f"user_session:{token} must be deleted after logout"
    )

    # A fresh unauthenticated client still 401s.
    with httpx.Client(base_url=ORCH, timeout=15.0) as anon2:
        r2 = anon2.get("/agents")
        assert r2.status_code == 401, f"post-logout unauth /agents must 401: {r2.status_code}"


# ── ST-UM.3 — Owner-filtered agent list ────────────────────────────────────


def test_st_um_3_owner_filtered_list(
    s7_seed,
    admin_client: httpx.Client,
    user_b_client: httpx.Client,
    rc: redis.Redis,
) -> None:
    """ST-UM.3 — Admin sees all agents; a fresh user that owns nothing does not.

    Asserts:
      * admin_client GET /agents includes the demo agent ``agent-sdk``.
      * user_b_client GET /agents does NOT include ``agent-sdk`` (user B owns
        nothing — likely []).
      * Redis agent:agent-sdk:owner is set (the agent has an owner at all).
    """
    def _agent_ids(resp: httpx.Response) -> set[str]:
        assert resp.status_code == 200, f"/agents must 200: {resp.status_code} {resp.text}"
        data = resp.json()
        # Tolerate either a bare list of ids or a list of objects with an id field.
        out: set[str] = set()
        for item in data:
            if isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict):
                out.add(item.get("agent_id") or item.get("id") or item.get("name"))
        return out

    admin_ids = _agent_ids(admin_client.get("/agents"))
    assert DEMO_AGENT in admin_ids, (
        f"admin must see all agents incl. {DEMO_AGENT}: {sorted(admin_ids)}"
    )

    user_b_ids = _agent_ids(user_b_client.get("/agents"))
    assert DEMO_AGENT not in user_b_ids, (
        f"user-b owns nothing, must not see {DEMO_AGENT}: {sorted(user_b_ids)}"
    )

    # The demo agent genuinely has an owner in Redis (some user, not unowned).
    assert rc.get(f"agent:{DEMO_AGENT}:owner"), (
        f"agent:{DEMO_AGENT}:owner must be set (the demo agent is owned)"
    )
