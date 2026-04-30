"""
Policy-enforcement addon — the single decision point.

Runs after SessionIdentity. For each request:

  1. Skip if already denied by a prior addon (flow.response is set).
  2. Skip if bypass (orchestrator /register traffic).
  3. Look up the agent's policy. No entry → deny `policy-not-found`.
  4. Classify the request by Host header:
       - LLM  (api.openai.com, api.anthropic.com)  → allowed_llm_providers check
       - MCP  (registered in Redis as mcp:{name})  → allowed_tools flat-list check
       - A2A  (in allowed_agents flat list)         → allowed by set membership
       - else                                       → deny `host-not-allowed`
  5. Per-turn limit pre-check (after classification, before returning allow):
       - Read session counters from Redis.
       - If limit exceeded: set amaze_budget_alert metadata, then block or
         pass through depending on on_budget_exceeded policy.
  6. On allow: inject trusted `x-amaze-caller: <agent_id>` header.

Any uncaught exception inside this addon is turned into a 403 (fail-closed)
by the addon loader in main.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis
from mitmproxy import http

from services.proxy import policy_store
from services.proxy._redis import client as redis_client
from services.proxy.deny import deny
from services.proxy.policy import (
    Policy,
    host_to_provider,
    is_llm_host,
)

logger = logging.getLogger(__name__)


class _RedisLookupError(RuntimeError):
    """Raised when Redis is unreachable — caller translates into a
    structured 503 deny so we don't classify as `host-not-allowed` when
    the underlying state store is down (fail-closed, correct reason)."""


class PolicyEnforcer:
    """Per-request policy refetch from Redis (S4-T2.2).

    No boot-time cache: every request hits Redis (`policy:{agent_id}`) so
    that orchestrator PUT /policy/{id} takes effect on the very next call
    without restart. Cost ≈ 1 GET per request.
    """

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return  # earlier addon already decided
        if flow.metadata.get("amaze_bypass"):
            return

        agent_id: str | None = flow.metadata.get("amaze_agent")
        if not agent_id:
            # Shouldn't happen — SessionIdentity either sets this or denies.
            # Defensive catch → fail closed.
            deny(flow, "no-identity")
            return

        try:
            policy = await policy_store.get_policy(agent_id)
        except redis.RedisError as e:
            logger.error("redis unreachable during policy lookup: %s", e)
            deny(flow, "redis-unavailable", status=503)
            return

        if policy is None:
            deny(flow, "policy-not-found", agent_id=agent_id)
            return

        host = flow.request.pretty_host

        # --- LLM classification -------------------------------------------
        if is_llm_host(host):
            provider = host_to_provider(host) or "unknown"
            if provider not in policy.allowed_llm_providers:
                deny(flow, "llm-not-allowed", provider=provider)
                return
            flow.metadata["amaze_kind"] = "llm"
            flow.metadata["amaze_llm_provider"] = provider
            self._inject_caller(flow, agent_id)
            # Per-turn token budget pre-check (LLM requests only)
            await self._check_per_turn_limit(
                flow, policy,
                counter_key_suffix="total_tokens",
                limit=policy.max_tokens_per_turn,
                field_name="max_tokens_per_turn",
            )
            return

        # --- MCP classification -------------------------------------------
        try:
            mcp_entry = await self._lookup_mcp(host)
        except _RedisLookupError:
            # Fail-closed with correct reason code.
            deny(flow, "redis-unavailable", status=503)
            return

        if mcp_entry is not None:
            # Set kind/server early so audit_log always sees kind="mcp" even
            # when the request is denied below (tool-not-allowed). Without this
            # the deny path returns before these are set and the audit record
            # falls through to kind="unknown" / target=pretty_host.
            flow.metadata["amaze_kind"] = "mcp"
            flow.metadata["amaze_mcp_server"] = host

            # Server-level check: is this MCP host registered at all in policy?
            # In the new flat-list schema the server allowlist is implicit —
            # the host must be registered in Redis as an MCP server (already
            # confirmed above) and either no tool is being called or the tool
            # must appear in allowed_tools.
            tool_name = self._extract_mcp_tool(flow)
            if tool_name is not None:
                if tool_name not in policy.allowed_tools:
                    deny(flow, "tool-not-allowed", server=host, tool=tool_name)
                    return
                flow.metadata["amaze_mcp_tool"] = tool_name
            self._inject_caller(flow, agent_id)
            # Per-turn tool-call budget pre-check
            await self._check_per_turn_limit(
                flow, policy,
                counter_key_suffix="total_tool_calls",
                limit=policy.max_tool_calls_per_turn,
                field_name="max_tool_calls_per_turn",
            )
            return

        # --- A2A classification -------------------------------------------
        if host in policy.allowed_agents:
            flow.metadata["amaze_kind"] = "a2a"
            flow.metadata["amaze_target"] = host
            self._inject_caller(flow, agent_id)
            # Per-turn agent-call budget pre-check
            await self._check_per_turn_limit(
                flow, policy,
                counter_key_suffix="total_agent_calls",
                limit=policy.max_agent_calls_per_turn,
                field_name="max_agent_calls_per_turn",
            )
            return

        # --- Unknown -------------------------------------------------------
        deny(flow, "host-not-allowed", host=host)

    # --- helpers ----------------------------------------------------------

    def _inject_caller(self, flow: http.HTTPFlow, agent_id: str) -> None:
        """Set the trusted x-amaze-caller header. Overwrites any prior value
        (which SessionIdentity already stripped)."""
        flow.request.headers["x-amaze-caller"] = agent_id

    async def _lookup_mcp(self, host: str) -> dict | None:
        """Return mcp:{host} payload from Redis, None if unregistered,
        raise _RedisLookupError if Redis itself is unreachable."""
        try:
            raw = await (await redis_client()).get(f"mcp:{host}")
        except redis.RedisError as e:
            logger.error("redis unreachable during mcp lookup: %s", e)
            raise _RedisLookupError from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _extract_mcp_tool(self, flow: http.HTTPFlow) -> str | None:
        """Pull `params.name` out of a JSON-RPC `tools/call` body.

        MCP streamable-http transport: client POSTs JSON-RPC to the server's
        endpoint. Tool invocation has:
            { "jsonrpc": "2.0", "method": "tools/call",
              "params": { "name": "<tool>", "arguments": {...} } }
        Non-invocation methods (initialize, tools/list, ...) return None —
        those are governed by the server-level check, not per-tool.
        """
        if flow.request.method != "POST":
            return None
        raw = flow.request.content or b""
        if not raw:
            return None
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        if body.get("method") != "tools/call":
            return None
        params = body.get("params") or {}
        name = params.get("name")
        return str(name) if name else None

    async def _check_per_turn_limit(
        self,
        flow: http.HTTPFlow,
        policy: Policy,
        counter_key_suffix: str,
        limit: int,
        field_name: str,
    ) -> None:
        """Read the session counter from Redis and enforce the per-turn limit.

        If limit == 0 the limit is disabled (unlimited).
        On Redis error: log a warning and skip the check (do not deny).
        Sets flow.metadata["amaze_budget_alert"] and optionally denies.
        Does nothing if flow.response is already set (prior step denied).
        """
        if limit == 0:
            return
        if flow.response is not None:
            return

        sid: str | None = flow.metadata.get("amaze_session")
        if not sid:
            return  # no session context yet — skip

        counter_key = f"session:{sid}:{counter_key_suffix}"
        try:
            r = await redis_client()
            raw_val = await r.get(counter_key)
        except redis.RedisError as e:
            logger.error(
                "redis error reading per-turn counter %s: %s — denying fail-closed",
                counter_key, e,
            )
            deny(flow, "redis-unavailable", status=503)
            return

        current: int = int(raw_val) if raw_val is not None else 0

        if current >= limit:
            alert: dict[str, Any] = {
                "field": field_name,
                "current": current,
                "limit": limit,
            }
            flow.metadata["amaze_budget_alert"] = alert
            if policy.on_budget_exceeded == "block":
                _reason_for = {
                    "max_tokens_per_turn": "budget_exceeded",
                    "max_tool_calls_per_turn": "tool-limit-exceeded",
                    "max_agent_calls_per_turn": "agent-limit-exceeded",
                }
                deny(flow, _reason_for.get(field_name, "budget_exceeded"), **alert)
            # on_budget_exceeded == "allow": alert metadata set, no deny
