"""
Policy-enforcement addon — the single decision point.

Runs after SessionIdentity. For each request:

  1. Skip if already denied by a prior addon (flow.response is set).
  2. Skip if bypass (orchestrator /register traffic).
  3. Look up the agent's policy. No entry → deny `policy-not-found`.
  4. Classify the request by Host header:
       - LLM  (api.openai.com, api.anthropic.com)  → allowed_llms check
       - MCP  (registered in Redis as mcp:{name})  → allowed_mcp_servers +
                                                     allowed_tools
       - A2A  (matches allowed_remote_agents)      → already allowed by set
                                                     membership
       - else                                      → deny `host-not-allowed`
  5. On allow: inject trusted `x-amaze-caller: <agent_id>` header.

Any uncaught exception inside this addon is turned into a 403 (fail-closed)
by the addon loader in main.py.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib

import redis.asyncio as redis
from mitmproxy import http

from services.proxy.deny import deny
from services.proxy.policy import (
    Policies,
    host_to_provider,
    is_llm_host,
    llm_model_allowed,
    load_policies,
)

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
CONFIG_DIR = pathlib.Path(os.environ.get("CONFIG_DIR", "/app/config"))


class PolicyEnforcer:
    def __init__(self) -> None:
        self._policies: Policies = load_policies(CONFIG_DIR / "policies.yaml")
        self._redis: redis.Redis | None = None

    async def _r(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

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

        policy = self._policies.get(agent_id)
        if policy is None:
            deny(flow, "policy-not-found", agent_id=agent_id)
            return

        host = flow.request.host

        # --- LLM classification -------------------------------------------
        if is_llm_host(host):
            provider = host_to_provider(host) or "unknown"
            model = self._extract_llm_model(flow)
            if model is None:
                deny(flow, "llm-model-missing", provider=provider)
                return
            if not llm_model_allowed(policy, provider, model):
                deny(flow, "llm-not-allowed", provider=provider, model=model)
                return
            flow.metadata["amaze_kind"] = "llm"
            flow.metadata["amaze_llm_provider"] = provider
            flow.metadata["amaze_llm_model"] = model
            self._inject_caller(flow, agent_id)
            return

        # --- MCP classification -------------------------------------------
        mcp_entry = await self._lookup_mcp(host)
        if mcp_entry is not None:
            if host not in policy.allowed_mcp_servers:
                deny(flow, "mcp-not-allowed", server=host)
                return
            # Tool allowlist: only enforced for tools/call invocations.
            tool_name = self._extract_mcp_tool(flow)
            if tool_name is not None:
                allowed = policy.allowed_tools.get(host, [])
                if tool_name not in allowed:
                    deny(flow, "tool-not-allowed", server=host, tool=tool_name)
                    return
                flow.metadata["amaze_mcp_tool"] = tool_name
            flow.metadata["amaze_kind"] = "mcp"
            flow.metadata["amaze_mcp_server"] = host
            self._inject_caller(flow, agent_id)
            return

        # --- A2A classification -------------------------------------------
        if host in policy.allowed_remote_agents:
            flow.metadata["amaze_kind"] = "a2a"
            flow.metadata["amaze_target"] = host
            self._inject_caller(flow, agent_id)
            return

        # --- Unknown ------------------------------------------------------
        deny(flow, "host-not-allowed", host=host)

    # --- helpers ----------------------------------------------------------

    def _inject_caller(self, flow: http.HTTPFlow, agent_id: str) -> None:
        """Set the trusted x-amaze-caller header. Overwrites any prior value
        (which SessionIdentity already stripped)."""
        flow.request.headers["x-amaze-caller"] = agent_id

    async def _lookup_mcp(self, host: str) -> dict | None:
        """Return mcp:{host} payload from Redis, or None if unregistered."""
        try:
            raw = await (await self._r()).get(f"mcp:{host}")
        except redis.RedisError as e:
            logger.error("redis unreachable during mcp lookup: %s", e)
            return None
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
        those are governed by the server-level allowlist, not per-tool.
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

    def _extract_llm_model(self, flow: http.HTTPFlow) -> str | None:
        """OpenAI / Anthropic: the model lives in the JSON request body."""
        raw = flow.request.content or b""
        if not raw:
            return None
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        model = body.get("model")
        return str(model) if model else None
