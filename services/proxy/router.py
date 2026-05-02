"""
Router addon — the last addon in the chain.

Resolves logical target names (e.g. "agent-sdk1", "demo-mcp") to their
registered host:port by reading from Redis, then rewrites
flow.request.host + port so mitmproxy connects to the real address.

Placement:
  Runs AFTER all enforcement and audit addons. The FailClosed wrapper in
  main.py guarantees it is skipped if any earlier addon has already denied
  the request (flow.response is set).

Call flow:
  PolicyEnforcer sets:
    flow.metadata["amaze_kind"]       = "a2a" | "mcp" | "llm"
    flow.metadata["amaze_target"]     = agent_id       (A2A only)
    flow.metadata["amaze_mcp_server"] = mcp_name       (MCP only)

  Router reads those tags and:
    A2A  → GET agent:{target}:endpoint  → "http://host:port"
    MCP  → GET mcp:{name}               → {"url": "http://host:port", ...}
    LLM  → no-op (mitmproxy forwards to real provider as-is)

Failure handling:
  Redis unavailable           → deny 503 "redis-unavailable"
  agent:{id}:endpoint missing → deny 503 "agent-not-registered"
  mcp:{name} missing          → deny 503 "mcp-not-registered"
    (PolicyEnforcer already checks MCP server existence, but Router
     guards again in case the key expired between the two checks.)
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import redis.asyncio as redis
from mitmproxy import http

from services.proxy._redis import client as redis_client
from services.proxy.deny import deny

logger = logging.getLogger(__name__)


def _parse_endpoint(url: str) -> tuple[str, int]:
    """Parse 'http://host:port[/...]' and return (host, port).

    Raises ValueError on malformed URLs or missing port.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port
    if not host:
        raise ValueError(f"no hostname in endpoint URL: {url!r}")
    if port is None:
        # Fall back to scheme default
        port = 443 if parsed.scheme == "https" else 80
    return host, port


class Router:
    """Rewrite flow.request.host + port to the registered endpoint address."""

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return  # already denied
        if flow.metadata.get("amaze_bypass"):
            return  # orchestrator registration traffic — don't touch

        kind: str | None = flow.metadata.get("amaze_kind")

        # LLM: no rewrite — mitmproxy MITM's the TLS tunnel to the real
        # provider address. Nothing to change here.
        if kind == "llm":
            return

        if kind == "a2a":
            await self._route_a2a(flow)
        elif kind == "mcp":
            await self._route_mcp(flow)
        # kind is None or unknown: PolicyEnforcer already denied the flow
        # before Router was called; the FailClosed guard means we only
        # reach here on allowed flows — so unknown kind is a bug, but
        # we leave the flow untouched rather than double-denying.

    # ------------------------------------------------------------------
    # A2A routing
    # ------------------------------------------------------------------

    async def _route_a2a(self, flow: http.HTTPFlow) -> None:
        target: str | None = flow.metadata.get("amaze_target")
        if not target:
            logger.error("amaze_kind=a2a but amaze_target is missing from metadata")
            deny(flow, "internal-error", status=403)
            return

        redis_key = f"agent:{target}:endpoint"
        endpoint_url = await self._redis_get(flow, redis_key)
        if flow.response is not None:
            return  # _redis_get denied on error

        if not endpoint_url:
            logger.warning("a2a endpoint not registered: key=%s", redis_key)
            deny(flow, "agent-not-registered", agent_id=target, status=503)
            return

        self._rewrite(flow, endpoint_url, context=f"a2a:{target}")

    # ------------------------------------------------------------------
    # MCP routing
    # ------------------------------------------------------------------

    async def _route_mcp(self, flow: http.HTTPFlow) -> None:
        server: str | None = flow.metadata.get("amaze_mcp_server")
        if not server:
            logger.error("amaze_kind=mcp but amaze_mcp_server is missing from metadata")
            deny(flow, "internal-error", status=403)
            return

        redis_key = f"mcp:{server}"
        raw = await self._redis_get(flow, redis_key)
        if flow.response is not None:
            return  # _redis_get denied on error

        if not raw:
            logger.warning("mcp server not registered: key=%s", redis_key)
            deny(flow, "mcp-not-registered", server=server, status=503)
            return

        try:
            entry = json.loads(raw)
            endpoint_url: str = entry["url"]
        except (ValueError, KeyError) as exc:
            logger.error("malformed mcp entry for %s: %s — %s", server, raw, exc)
            deny(flow, "internal-error", status=403)
            return

        self._rewrite(flow, endpoint_url, context=f"mcp:{server}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _redis_get(self, flow: http.HTTPFlow, key: str) -> str | None:
        """Fetch *key* from Redis. On RedisError, deny the flow and return None.

        Callers must check `flow.response is not None` after calling this.
        """
        try:
            r = await redis_client()
            return await r.get(key)
        except redis.RedisError as exc:
            logger.error("redis unavailable reading %s: %s", key, exc)
            deny(flow, "redis-unavailable", status=503)
            return None

    def _rewrite(self, flow: http.HTTPFlow, endpoint_url: str, context: str) -> None:
        """Parse *endpoint_url* and overwrite flow.request.host, port, and path prefix."""
        try:
            host, port = _parse_endpoint(endpoint_url)
        except ValueError as exc:
            logger.error("cannot parse endpoint for %s: %s", context, exc)
            deny(flow, "internal-error", status=403)
            return

        # Normalise trailing slash: if the request path is exactly canonical_path + "/"
        # (nothing after), rewrite to canonical_path.  This prevents 307 redirects from
        # servers like Starlette (redirect_slashes=True) and works regardless of where
        # the upstream is hosted.
        canonical_path = urlparse(endpoint_url).path.rstrip("/")
        if canonical_path and flow.request.path == canonical_path + "/":
            flow.request.path = canonical_path

        logger.debug(
            "router rewrite %s: %s → %s:%s",
            context,
            flow.request.pretty_host,
            host,
            port,
        )
        flow.request.host = host
        flow.request.port = port
