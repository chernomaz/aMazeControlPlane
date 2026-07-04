"""
Shared pytest fixtures for all tests in this directory.

Registers A2A endpoint keys in Redis so the Router addon can route A2A
calls in both S2 and S3 tests without requiring real SDK processes.

Why this file exists
--------------------
The Router addon (added in S3) looks up `agent:{id}:endpoint` in Redis
before forwarding any A2A request. Without a registered endpoint the Router
denies with 503 `agent-not-registered` — correct for production, but it
breaks S2 tests where mock services were never expected to self-register.

This autouse session-scoped fixture registers the endpoints once before any
test runs, without modifying individual test files.
"""
from __future__ import annotations

import os
import subprocess

import httpx
import pytest

ORCH = os.environ.get("AMAZE_ORCHESTRATOR", "http://localhost:18001")

# ── S7 auth fixtures ────────────────────────────────────────────────────────
# The S7 user-management tests run against the LIVE dev stack (amaze-platform on
# :8001, same as test_s4.py), not the test-compose stack. We seed deterministic
# principals via the container's own `auth` module (so we never depend on the
# auto-generated bootstrap-admin password), then hand back logged-in clients.

S7_ORCH = os.environ.get("AMAZE_ORCHESTRATOR_S7", "http://localhost:8001")
S7_CONTAINER = os.environ.get("AMAZE_PLATFORM_CONTAINER", "amaze-platform")

# (username, password, role) — idempotently created before the S7 suite runs.
# s7-root is the dedicated test ADMIN. s7-admin was demoted to a regular user
# (S8): the human-facing super-admin is now `admin`/`admin`, and a normal admin
# account should not be a phantom owner of every agent.
S7_USERS = [
    ("s7-root", "s7-root-pass", "admin"),
    ("s7-admin", "s7-admin-pass", "user"),
    ("s7-user-a", "s7-a-pass", "user"),
    ("s7-user-b", "s7-b-pass", "user"),
]


def _seed_user(username: str, password: str, role: str) -> None:
    """Create a user inside the platform container via its auth module, and
    NORMALISE its role on every run. Idempotent: a 409 (username taken) is
    swallowed, then the role is force-set so a pre-existing principal whose role
    changed across sprints (e.g. s7-admin admin→user) converges to S7_USERS."""
    code = (
        "import asyncio, os, redis.asyncio as redis\n"
        "from services.orchestrator import auth\n"
        "async def m():\n"
        "    r = redis.from_url(os.environ['REDIS_URL'], decode_responses=True)\n"
        f"    try: await auth.create_user(r, {username!r}, {password!r}, role={role!r})\n"
        "    except Exception as e: print('seed-skip', type(e).__name__)\n"
        f"    uid = await r.get('username:' + {username!r})\n"
        f"    if uid: await r.hset('user:' + uid, 'role', {role!r})\n"
        "asyncio.run(m())\n"
    )
    subprocess.run(
        ["docker", "exec", S7_CONTAINER, "python", "-c", code],
        check=False, capture_output=True, text=True,
    )


def _amaze_stack_up() -> bool:
    try:
        return httpx.get(f"{S7_ORCH}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def s7_seed():
    """Seed the S7 principals once. Skips the whole S7 suite if the stack
    is down (mirrors the skip-on-stack-down convention of test_s6)."""
    if not _amaze_stack_up():
        pytest.skip("amaze-platform stack not up on :8001")
    for u, p, role in S7_USERS:
        _seed_user(u, p, role)


def _login(username: str, password: str) -> httpx.Client:
    c = httpx.Client(base_url=S7_ORCH, timeout=15.0)
    resp = c.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"login {username} failed: {resp.text}"
    return c  # session cookie now lives in the client's jar


@pytest.fixture
def admin_client(s7_seed):
    c = _login("s7-root", "s7-root-pass")
    yield c
    c.close()


@pytest.fixture
def user_a_client(s7_seed):
    c = _login("s7-user-a", "s7-a-pass")
    yield c
    c.close()


@pytest.fixture
def user_b_client(s7_seed):
    c = _login("s7-user-b", "s7-b-pass")
    yield c
    c.close()

# The mock-agent container (compose.test.yml) is the real upstream for A2A.
# It listens on port 8000, reachable inside Docker via the service name
# "mock-agent". The Router rewrites the host+port from this registration.
_MOCK_AGENT_HOST = "mock-agent"
_MOCK_AGENT_PORT = 8000


@pytest.fixture(scope="session", autouse=True)
def register_a2a_endpoints() -> None:
    """Register A2A endpoint keys for all test agents that act as A2A targets.

    Agents registered here:
      - test-a2a-callee  → used by S2 tests (ST-S2.10, ST-S2.12, ST-S2.13)
      - test-s3-callee   → used by S3 tests (ST-S3.4, ST-S3.5)

    Registration stores `agent:{id}:endpoint` in Redis (via the orchestrator's
    POST /register endpoint). The bearer token + session_id returned are
    discarded — only the endpoint key matters for routing.

    Status codes 200 / 201 / 409 are all acceptable (idempotent re-runs).
    """
    _endpoints = [
        # Used by S2 tests (ST-S2.10, ST-S2.12, ST-S2.13): A2A callee + LLM caller
        ("test-a2a-callee", _MOCK_AGENT_HOST, _MOCK_AGENT_PORT),
        # Used by S3 tests (ST-S3.4, ST-S3.5): Router routing target
        ("test-s3-callee",  _MOCK_AGENT_HOST, _MOCK_AGENT_PORT),
    ]
    try:
        client = httpx.Client(base_url=ORCH, timeout=5.0)
    except Exception:
        return
    with client as c:
        for agent_id, host, port in _endpoints:
            try:
                resp = c.post(
                    "/register",
                    json={
                        "agent_id": agent_id,
                        "a2a_host": host,
                        "a2a_port": port,
                    },
                )
            except httpx.HTTPError:
                # Test-compose orchestrator (:18001) not up — the S2/S3 tests
                # that need it skip on their own; don't block other suites
                # (e.g. S7 against the live :8001 stack).
                return
            assert resp.status_code in (200, 201, 409), (
                f"endpoint registration failed for {agent_id}: {resp.text}"
            )
