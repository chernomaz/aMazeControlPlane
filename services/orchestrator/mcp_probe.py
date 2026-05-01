"""MCP auto-probe: discover tools from a FastMCP streamable-http server.

FastMCP uses "streamable-http" transport:
  - All calls are POST with a JSON-RPC body.
  - Responses come back as SSE (Content-Type: text/event-stream) with one
    `event: message\\ndata: {...}` block per request.

Handshake (three steps):
  1. initialize  — POST JSON-RPC, parse SSE response, extract mcp-session-id header
  2. notifications/initialized — POST notify (no response body needed, 202 expected)
  3. tools/list  — POST JSON-RPC, parse SSE response, extract result.tools

Usage:
    from mcp_probe import probe_tools, ProbeError

    try:
        tools = await probe_tools("http://localhost:8000/mcp")
    except ProbeError as e:
        # handle error
"""
from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)


class ProbeError(Exception):
    """Raised on any failure during MCP probing (connection, protocol, parse)."""


def _parse_sse_data(body: str) -> dict:
    """Extract the JSON payload from an SSE response body.

    SSE format:
        event: message
        data: {"jsonrpc": "2.0", ...}

    Skips blank lines and `event:` lines; parses the first `data:` line found.
    Raises ProbeError if no data line is found or JSON is malformed.
    """
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ProbeError(f"SSE data is not valid JSON: {exc} — raw: {payload!r}") from exc
    raise ProbeError(f"No SSE data line found in response body: {body!r}")


async def probe_tools(url: str, timeout: float = 5.0) -> list[dict]:
    """Probe a FastMCP streamable-http server and return its tool list.

    Args:
        url:     Base URL of the MCP server (e.g. ``http://localhost:8000/mcp``).
                 A trailing slash is stripped before use; FastMCP returns a 307
                 redirect from ``/mcp/`` to ``/mcp``, so redirects are followed.
        timeout: Per-request timeout in seconds (default 5.0).

    Returns:
        List of tool dicts, each with keys as returned by the MCP server:
        ``{"name": str, "description": str | None, "inputSchema": dict | None}``.
        Extra keys present in the server response are preserved.

    Raises:
        ProbeError: On any failure — connection error, unexpected HTTP status,
                    missing session-id header, JSON/SSE parse error, or the
                    server response missing the ``result.tools`` key.
    """
    url = url.rstrip("/")

    headers_json_sse = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers_json = {
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
        ) as client:

            # ----------------------------------------------------------------
            # Step 1: initialize
            # ----------------------------------------------------------------
            init_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "amaze-probe",
                        "version": "1.0.0",
                    },
                },
            }
            try:
                resp1 = await client.post(url, json=init_body, headers=headers_json_sse)
            except httpx.ConnectError as exc:
                raise ProbeError(f"Cannot connect to MCP server at {url}: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise ProbeError(f"Timeout connecting to MCP server at {url}: {exc}") from exc
            except httpx.RequestError as exc:
                raise ProbeError(f"Request error during initialize: {exc}") from exc

            if resp1.status_code != 200:
                raise ProbeError(
                    f"initialize returned HTTP {resp1.status_code} "
                    f"(expected 200): {resp1.text[:200]!r}"
                )

            session_id = resp1.headers.get("mcp-session-id")
            if not session_id:
                raise ProbeError(
                    "initialize response missing 'mcp-session-id' header; "
                    f"response headers: {dict(resp1.headers)}"
                )

            init_data = _parse_sse_data(resp1.text)
            if "error" in init_data:
                raise ProbeError(
                    f"initialize returned JSON-RPC error: {init_data['error']}"
                )

            logger.debug("mcp_probe: initialize OK, session_id=%s", session_id)

            # ----------------------------------------------------------------
            # Step 2: notifications/initialized  (fire-and-forget notify)
            # ----------------------------------------------------------------
            notify_body = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            # FastMCP requires Accept: application/json, text/event-stream on
            # every request — including fire-and-forget notifications.
            headers_notify = {
                **headers_json_sse,
                "mcp-session-id": session_id,
            }
            try:
                resp2 = await client.post(url, json=notify_body, headers=headers_notify)
            except httpx.RequestError as exc:
                raise ProbeError(
                    f"Request error during notifications/initialized: {exc}"
                ) from exc

            if resp2.status_code not in (200, 202, 204):
                raise ProbeError(
                    f"notifications/initialized returned HTTP {resp2.status_code} "
                    f"(expected 202): {resp2.text[:200]!r}"
                )

            logger.debug("mcp_probe: notifications/initialized OK (HTTP %s)", resp2.status_code)

            # ----------------------------------------------------------------
            # Step 3: tools/list
            # ----------------------------------------------------------------
            list_body = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
            headers_list = {
                **headers_json_sse,
                "mcp-session-id": session_id,
            }
            try:
                resp3 = await client.post(url, json=list_body, headers=headers_list)
            except httpx.RequestError as exc:
                raise ProbeError(
                    f"Request error during tools/list: {exc}"
                ) from exc

            if resp3.status_code != 200:
                raise ProbeError(
                    f"tools/list returned HTTP {resp3.status_code} "
                    f"(expected 200): {resp3.text[:200]!r}"
                )

            list_data = _parse_sse_data(resp3.text)
            if "error" in list_data:
                raise ProbeError(
                    f"tools/list returned JSON-RPC error: {list_data['error']}"
                )

            result = list_data.get("result")
            if result is None:
                raise ProbeError(
                    f"tools/list response missing 'result' key: {list_data!r}"
                )

            tools = result.get("tools")
            if tools is None:
                raise ProbeError(
                    f"tools/list result missing 'tools' key: {result!r}"
                )

            if not isinstance(tools, list):
                raise ProbeError(
                    f"tools/list 'tools' is not a list: {type(tools).__name__}"
                )

            logger.debug("mcp_probe: tools/list OK, %d tool(s) found", len(tools))
            return tools

    except ProbeError:
        raise
    except Exception as exc:  # noqa: BLE001 — catch-all to surface unexpected errors
        raise ProbeError(f"Unexpected error while probing {url}: {exc}") from exc
