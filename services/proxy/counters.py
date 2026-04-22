"""
Request / token counters.

Runs on REQUEST (increment call counters) and RESPONSE (extract token usage
from LLM responses). All writes are atomic via Redis INCR / INCRBY.

Redis keys written:
  session:{sid}:mcp:{server}:{tool}        INCR      per-tool invocation count
  session:{sid}:a2a:{target}               INCR      per-target A2A call count
  session:{sid}:llm:{provider}:{model}     INCR      per-model call count
  session:{sid}:llm_tokens:{provider}:{model}  INCRBY prompt+completion tokens
  agent:{agent_id}:total_requests          INCR      grand total, all sessions
  agent:{agent_id}:total_llm_tokens        INCRBY    grand total tokens

The session id is read from `flow.metadata["amaze_session"]` — set once by
the SessionIdentity addon. Previously this module re-fetched
`agent_session:{agent_id}` per request, which was two extra Redis round-
trips on the hot path.
"""
from __future__ import annotations

import json
import logging

import redis.asyncio as redis
from mitmproxy import http

from services.proxy._redis import client as redis_client

logger = logging.getLogger(__name__)


class Counters:
    # --- request-side counters: only count what was ALLOWED ---------------

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return  # denied — don't count denied traffic here
        agent_id = flow.metadata.get("amaze_agent")
        if not agent_id:
            return  # bypass or unidentified; nothing to count

        kind = flow.metadata.get("amaze_kind")
        if kind is None:
            return

        sid = flow.metadata.get("amaze_session")
        r = await redis_client()
        pipe = r.pipeline()
        pipe.incr(f"agent:{agent_id}:total_requests")

        if kind == "mcp":
            server = flow.metadata.get("amaze_mcp_server")
            tool = flow.metadata.get("amaze_mcp_tool")
            if sid and server and tool:
                pipe.incr(f"session:{sid}:mcp:{server}:{tool}")
        elif kind == "a2a":
            target = flow.metadata.get("amaze_target")
            if sid and target:
                pipe.incr(f"session:{sid}:a2a:{target}")
        elif kind == "llm":
            provider = flow.metadata.get("amaze_llm_provider")
            model = flow.metadata.get("amaze_llm_model")
            if sid and provider and model:
                pipe.incr(f"session:{sid}:llm:{provider}:{model}")

        try:
            await pipe.execute()
        except redis.RedisError as e:
            logger.warning("counter write failed: %s", e)

    # --- response-side: extract token usage from LLM responses ------------

    async def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("amaze_kind") != "llm":
            return
        agent_id = flow.metadata.get("amaze_agent")
        provider = flow.metadata.get("amaze_llm_provider")
        model = flow.metadata.get("amaze_llm_model")
        if not (agent_id and provider and model):
            return

        tokens = _parse_llm_tokens(flow)
        if tokens is None:
            return

        sid = flow.metadata.get("amaze_session")
        r = await redis_client()
        pipe = r.pipeline()
        pipe.incrby(f"agent:{agent_id}:total_llm_tokens", tokens)
        if sid:
            pipe.incrby(f"session:{sid}:llm_tokens:{provider}:{model}", tokens)
        try:
            await pipe.execute()
        except redis.RedisError as e:
            logger.warning("token counter write failed: %s", e)


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
