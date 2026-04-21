"""
Session-identity addon.

Runs FIRST on every request:
  1. Reads `Authorization: Bearer <token>`.
  2. Resolves `session_token:{token}` → agent_id in Redis.
  3. Writes agent_id into `flow.metadata["amaze_agent"]` for downstream addons.
  4. Strips any client-supplied `x-amaze-caller` header (spoof-prevention).
     The trusted value is injected later by the enforcer addon on allow.

Any failure path denies the request with a stable `reason` code:
  - no `Authorization` header / not `Bearer …` → `invalid-bearer`
  - bearer doesn't resolve in Redis              → `invalid-bearer`
  - Redis unreachable                            → `redis-unavailable` (503)
  - anything else                                → `internal-error`

Bearer check is skipped for requests to the orchestrator itself (so
`POST /register` doesn't require a bearer). The orchestrator is reachable
only over the internal network, so this is not a public surface.
"""
from __future__ import annotations

import logging
import os

import redis.asyncio as redis
from mitmproxy import http

from services.proxy.deny import deny

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")

# Hostnames that bypass bearer resolution. The orchestrator self-registration
# surface is reachable by siblings at `http://amaze:8001/register` — no bearer
# available yet (the agent hasn't registered). Also exclude mitmproxy's own
# internal URL.
_BEARER_BYPASS_HOSTS: set[str] = {"amaze"}


class SessionIdentity:
    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    async def _r(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

    async def request(self, flow: http.HTTPFlow) -> None:
        # Strip spoofed headers first — regardless of what happens next, the
        # value an agent might have set must never reach the upstream. Both
        # the caller id and our own bearer header are consumed here and
        # never forwarded.
        flow.request.headers.pop("x-amaze-caller", None)
        # Note: X-Amaze-Bearer is read below; stripped after resolution so
        # it never reaches the upstream API.

        if flow.request.host in _BEARER_BYPASS_HOSTS:
            flow.metadata["amaze_bypass"] = True
            return

        # Our identity header is X-Amaze-Bearer — NOT Authorization. Reason:
        # Authorization is reserved by the LLM/MCP provider itself (OpenAI's
        # API key, Anthropic's x-api-key, etc.). Using a dedicated header
        # means the proxy can resolve caller identity without conflicting
        # with the agent's own auth to the upstream.
        token = flow.request.headers.get("X-Amaze-Bearer", "").strip()
        if not token:
            deny(flow, "invalid-bearer")
            return

        try:
            agent_id = await (await self._r()).get(f"session_token:{token}")
        except redis.RedisError as e:
            logger.error("redis unreachable during bearer lookup: %s", e)
            deny(flow, "redis-unavailable", status=503)
            return

        if not agent_id:
            deny(flow, "invalid-bearer")
            return

        flow.metadata["amaze_agent"] = agent_id
        # Strip the bearer before forwarding — upstream never sees it.
        flow.request.headers.pop("X-Amaze-Bearer", None)
