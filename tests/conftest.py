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

import httpx
import pytest

ORCH = os.environ.get("AMAZE_ORCHESTRATOR", "http://localhost:18001")

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
    with httpx.Client(base_url=ORCH, timeout=15.0) as c:
        for agent_id, host, port in _endpoints:
            resp = c.post(
                "/register",
                json={
                    "agent_id": agent_id,
                    "a2a_host": host,
                    "a2a_port": port,
                },
            )
            assert resp.status_code in (200, 201, 409), (
                f"endpoint registration failed for {agent_id}: {resp.text}"
            )
