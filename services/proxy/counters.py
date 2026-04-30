"""
Request / token counters — Sprint S2 T2-9.

Request hook: time-window rate-limit pre-check (LLM only) via RedisTimeSeries.
Response hook: per-turn INCR counters (read by enforcer) + TS.ADD time-series
               metrics (read by dashboards and the request-hook pre-check).

Redis keys written:

  Per-turn (simple integers, read by enforcer):
    session:{sid}:total_tool_calls    INCR   MCP tool invocations
    session:{sid}:total_agent_calls   INCR   A2A call count
    session:{sid}:total_tokens        INCRBY LLM prompt+completion tokens

  Time-series (TS.ADD, auto-created on first write):
    ts:{agent_id}:llm_tokens          token count per LLM response
    ts:{agent_id}:tool_calls          1 per MCP tool call
    ts:{agent_id}:a2a_calls           1 per A2A call
    ts:{agent_id}:denials             1 per denied request

The session id is read from `flow.metadata["amaze_session"]` — set once by
the SessionIdentity addon.
"""
from __future__ import annotations

import json
import logging
import time

import redis.asyncio as redis
from mitmproxy import http

from services.proxy import policy_store
from services.proxy._redis import client as redis_client
from services.proxy.deny import deny

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window parser
# ---------------------------------------------------------------------------

def _parse_window_ms(window: str) -> int:
    """Convert a window string like '10m', '1h', '30s' to milliseconds."""
    if window.endswith("m"):
        return int(window[:-1]) * 60_000
    if window.endswith("h"):
        return int(window[:-1]) * 3_600_000
    if window.endswith("s"):
        return int(window[:-1]) * 1_000
    raise ValueError(f"unknown window format: {window!r}")


# ---------------------------------------------------------------------------
# Addon
# ---------------------------------------------------------------------------

class Counters:

    # --- request hook: rate-limit pre-check (LLM only) ---------------------

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return  # already denied upstream
        if flow.metadata.get("amaze_bypass"):
            return

        agent_id = flow.metadata.get("amaze_agent")
        kind = flow.metadata.get("amaze_kind")
        if agent_id is None or kind is None:
            return

        # Rate-limit pre-check applies only to LLM requests.
        if kind != "llm":
            return

        try:
            policy = await policy_store.get_policy(agent_id)
        except redis.RedisError as e:
            logger.error("counters: policy fetch failed — denying fail-closed: %s", e)
            deny(flow, "redis-unavailable", status=503)
            return
        if policy is None:
            return  # enforcer handles policy-not-found

        if not policy.token_rate_limits:
            return

        try:
            r = await redis_client()
            now_ms = int(time.time() * 1000)

            for rate_limit in policy.token_rate_limits:
                window_str = rate_limit.window
                window_ms = _parse_window_ms(window_str)
                from_ts = now_ms - window_ms

                try:
                    values = await r.ts().range(
                        f"ts:{agent_id}:llm_tokens", from_ts, "+"
                    )
                    total = sum(v for _, v in values)
                except redis.ResponseError:
                    # Key does not exist yet — treat as zero usage.
                    total = 0

                if total >= rate_limit.max_tokens:
                    flow.metadata["amaze_rate_alert"] = {
                        "window": window_str,
                        "current": total,
                        "limit": rate_limit.max_tokens,
                    }
                    if policy.on_budget_exceeded == "block":
                        deny(
                            flow,
                            "rate-limit-exceeded",
                            window=window_str,
                            current=total,
                            limit=rate_limit.max_tokens,
                        )
                        return
                    # on_budget_exceeded == "allow": alert metadata set, pass through

        except redis.RedisError as e:
            logger.error("rate-limit pre-check failed — denying fail-closed: %s", e)
            deny(flow, "redis-unavailable", status=503)
            return

    # --- response hook: per-turn INCR + TS.ADD time-series metrics ----------

    async def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("amaze_bypass"):
            return

        agent_id = flow.metadata.get("amaze_agent")
        kind = flow.metadata.get("amaze_kind")
        sid = flow.metadata.get("amaze_session")

        if not agent_id or not kind:
            return

        r = await redis_client()
        is_denied = flow.response is not None and flow.response.status_code >= 400

        # -- 1. Per-turn integer counters (enforcer reads these) -------------
        # Only count calls that actually reached the upstream; denied requests
        # must not inflate the counters used by subsequent enforcement checks.
        if not is_denied:
            try:
                if kind == "mcp" and flow.metadata.get("amaze_mcp_tool"):
                    if sid:
                        await r.incr(f"session:{sid}:total_tool_calls")
                elif kind == "a2a":
                    if sid:
                        await r.incr(f"session:{sid}:total_agent_calls")
                elif kind == "llm":
                    tokens = _parse_llm_tokens(flow)
                    if tokens is not None and sid:
                        await r.incrby(f"session:{sid}:total_tokens", tokens)
            except redis.RedisError as e:
                logger.warning("per-turn counter write failed: %s", e)

        # -- 2. Time-series metrics (TS.ADD) — only for calls reaching upstream
        if not is_denied:
            try:
                if kind == "mcp":
                    await r.ts().add(f"ts:{agent_id}:tool_calls", "*", 1)
                elif kind == "a2a":
                    await r.ts().add(f"ts:{agent_id}:a2a_calls", "*", 1)
                elif kind == "llm":
                    tokens = _parse_llm_tokens(flow)
                    if tokens is not None:
                        await r.ts().add(f"ts:{agent_id}:llm_tokens", "*", tokens)
            except redis.RedisError as e:
                logger.warning("TS.ADD metric write failed: %s", e)

        # -- 3. Denial counter ------------------------------------------------
        if is_denied:
            try:
                await r.ts().add(f"ts:{agent_id}:denials", "*", 1)
            except redis.RedisError as e:
                logger.warning("TS.ADD denial counter failed: %s", e)


# ---------------------------------------------------------------------------
# Token-count extractor (unchanged)
# ---------------------------------------------------------------------------

def _parse_llm_tokens(flow: http.HTTPFlow) -> int | None:
    """Return prompt_tokens + completion_tokens from an OpenAI/Anthropic
    response body, or None if not parseable / streaming."""
    raw = flow.response.content if flow.response else None
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    # OpenAI: {"usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}}
    # Anthropic: {"usage": {"input_tokens": N, "output_tokens": N}}
    usage = body.get("usage") or {}
    if "total_tokens" in usage:
        return int(usage["total_tokens"])
    total = 0
    for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"):
        v = usage.get(key)
        if isinstance(v, (int, float)):
            total += int(v)
    return total if total > 0 else None
