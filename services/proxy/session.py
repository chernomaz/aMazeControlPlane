"""
Session-identity addon.

Runs FIRST on every request:
  1. Unconditionally strips client-supplied `x-amaze-caller` AND
     `X-Amaze-Bearer` from the outbound headers — neither may ever leave
     the proxy. The bearer is read into a local variable BEFORE the
     strip so we still see it for the lookup below.
  2. If the Host is in the bypass set (e.g. `amaze` for orchestrator
     `/register` calls) marks the flow and returns.
  3. Resolves `session_token:{token}` → agent_id in Redis.
  4. Also fetches `agent_session:{agent_id}` so downstream addons can
     attribute counters to a session without a second round-trip.
  5. Stashes both in `flow.metadata` as `amaze_agent` and `amaze_session`.

Failure paths deny with stable reason codes:
  - missing bearer / not resolvable  → 403 `invalid-bearer`
  - Redis unreachable                → 503 `redis-unavailable`
"""
from __future__ import annotations

import logging
import re

import redis.asyncio as redis
from mitmproxy import http

from services.proxy._redis import client as redis_client
from services.proxy.deny import deny

logger = logging.getLogger(__name__)

# Debug-user ids are client-supplied and get interpolated into debug:* Redis
# key names by the debug_pauser. Constrain to a safe charset/length (UUIDs from
# the UI pass); anything else is treated as "no user" → never parked (fail safe).
_DEBUG_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Hostnames that bypass bearer resolution. `amaze` is the compose DNS name
# for the platform container — siblings hit `http://amaze:8001/register`
# before they have a bearer. Internal-only net; not a public surface.
_BEARER_BYPASS_HOSTS: frozenset[str] = frozenset({"amaze"})


class SessionIdentity:
    async def request(self, flow: http.HTTPFlow) -> None:
        # Capture bearer BEFORE stripping so we can still resolve it.
        bearer = flow.request.headers.get("X-Amaze-Bearer", "").strip()
        # Capture the debug-user the same way — read before the scrub so a
        # sibling addon (enforcer) can re-inject it for A2A peers only.
        debug_user = flow.request.headers.get("X-Amaze-Debug-User", "").strip()

        # Unconditional scrub — upstream NEVER sees these, regardless of
        # which branch we exit on. Covers C1 (bearer leak on bypass path)
        # and the pre-existing x-amaze-caller spoof-prevention.
        flow.request.headers.pop("x-amaze-caller", None)
        flow.request.headers.pop("X-Amaze-Bearer", None)
        flow.request.headers.pop("X-Amaze-Debug-User", None)

        # Stash unconditionally so every downstream addon sees it on all
        # paths (bypass, deny, success). None when the header was absent or
        # not a safe id — an unsafe value is dropped (never parked) rather than
        # used to build a malformed/colliding debug key.
        flow.metadata["amaze_debug_user"] = (
            debug_user if _DEBUG_USER_RE.match(debug_user) else None
        )

        if flow.request.host in _BEARER_BYPASS_HOSTS:
            flow.metadata["amaze_bypass"] = True
            return

        if not bearer:
            deny(flow, "invalid-bearer")
            return

        try:
            r = await redis_client()
            # One pipeline: token→agent + agent→session. Saves a round-trip
            # later (S1 — counters would otherwise do the second GET again).
            pipe = r.pipeline()
            pipe.get(f"session_token:{bearer}")
            # The agent_id-keyed lookup needs the resolved agent_id — we
            # can't pipeline both since the second depends on the first.
            agent_id = (await pipe.execute())[0]
        except redis.RedisError as e:
            logger.error("redis unreachable during bearer lookup: %s", e)
            deny(flow, "redis-unavailable", status=503)
            return

        if not agent_id:
            deny(flow, "invalid-bearer")
            return

        try:
            session_id = await (await redis_client()).get(
                f"agent_session:{agent_id}"
            )
        except redis.RedisError as e:
            logger.error("redis unreachable during session lookup: %s", e)
            deny(flow, "redis-unavailable", status=503)
            return

        flow.metadata["amaze_agent"] = agent_id
        # session_id can legitimately be None if the TTL has expired — we
        # still allow the request (bearer is the auth), but counters skip
        # the session-keyed writes.
        flow.metadata["amaze_session"] = session_id
