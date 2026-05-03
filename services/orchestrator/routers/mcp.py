"""GET /mcp_servers — listing of registered MCP servers and their tools.

Source:
  Redis `mcp:{name}` keys hold `json({url, tools})` (set by /register?kind=mcp
  or seeded from mcp_servers.yaml at orchestrator boot — see main._bootstrap_*).
  Approval gate `mcp:{name}:approved` (Phase 2): default approved unless the
  value is explicitly "false".

Mutations (S4-T2.1, S4-T2.4):
  POST /mcp_servers                  — manual registration (creates `mcp:{name}`)
  POST /mcp_servers/{name}/approve   — write `mcp:{name}:approved` = "true"
  POST /mcp_servers/{name}/reject    — write `mcp:{name}:approved` = "false"
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..mcp_probe import ProbeError, probe_tools

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/mcp_servers")
async def list_mcp_servers(request: Request) -> list[dict[str, Any]]:
    r = request.app.state.redis
    out: list[dict[str, Any]] = []
    try:
        async for key in r.scan_iter(match="mcp:*"):
            # Skip auxiliary keys like `mcp:{name}:approved` — only the
            # bare `mcp:{name}` entries hold the {url, tools} payload.
            if key.count(":") != 1:
                continue
            name = key[len("mcp:"):]
            raw = await r.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning("mcp_servers: malformed payload for %s", name)
                continue

            approved_flag = await r.get(f"mcp:{name}:approved")
            approved = approved_flag != "false"  # default approved

            raw_tools = data.get("tools", [])
            tools: list[dict] = []
            for t in raw_tools:
                if isinstance(t, str):
                    tools.append({"name": t})
                elif isinstance(t, dict):
                    tools.append(t)
                # else: skip malformed entries

            out.append({
                "name": name,
                "url": data.get("url", ""),
                "tools": tools,
                "approved": approved,
            })
    except Exception as e:  # noqa: BLE001
        logger.error("mcp_servers: redis scan failed: %s", e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    out.sort(key=lambda x: x["name"])
    return out


# --- Manual registration (S4-T2.4) ---------------------------------------

class McpRegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1)
    tools: Optional[list[str]] = None  # None → auto-probe; list → manual override

    @field_validator("name")
    @classmethod
    def _name_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must be non-empty")
        return v

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, v: str) -> str:
        # Accept http/https only — matches the proxy's router upstream resolver.
        # Avoid pydantic's HttpUrl which normalizes (adds trailing slash) and
        # would surprise tests that compare back the URL byte-for-byte.
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be http(s)://host[:port][/path]")
        return v


@router.post("/mcp_servers", status_code=201)
async def create_mcp_server(body: McpRegisterIn, request: Request) -> dict[str, Any]:
    r = request.app.state.redis

    # Resolve tool list — probe the server when none provided manually.
    if body.tools is None:
        try:
            tool_objects: list[dict] = await probe_tools(body.url)
        except ProbeError as e:
            raise HTTPException(status_code=502, detail=f"mcp-probe-failed: {e}") from e
    else:
        # Manual override — wrap bare strings as minimal {name} objects.
        tool_objects = [{"name": t} for t in body.tools]

    payload = json.dumps({"url": body.url, "tools": tool_objects})
    try:
        # SET with NX → only create if absent. Then mark approved.
        ok = await r.set(f"mcp:{body.name}", payload, nx=True)
        if not ok:
            raise HTTPException(status_code=409, detail="mcp-already-exists")
        await r.set(f"mcp:{body.name}:approved", "true")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — fail closed on Redis errors
        logger.error("mcp_servers: redis write failed for %s: %s", body.name, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    logger.info("mcp_servers: registered name=%s url=%s tools=%d",
                body.name, body.url, len(tool_objects))
    return {
        "name": body.name,
        "url": body.url,
        "tools": tool_objects,
        "approved": True,
    }


# --- Approve / reject (S4-T2.1) ------------------------------------------

async def _set_mcp_approved(request: Request, name: str, approved: bool) -> dict[str, Any]:
    r = request.app.state.redis
    try:
        if not await r.exists(f"mcp:{name}"):
            raise HTTPException(status_code=404, detail="mcp-not-found")
        await r.set(f"mcp:{name}:approved", "true" if approved else "false")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — fail closed on Redis errors
        logger.error("mcp_servers: redis write failed for %s: %s", name, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    return {"name": name, "approved": approved}


@router.post("/mcp_servers/{name}/approve")
async def approve_mcp_server(name: str, request: Request) -> dict[str, Any]:
    return await _set_mcp_approved(request, name, True)


@router.post("/mcp_servers/{name}/reject")
async def reject_mcp_server(name: str, request: Request) -> dict[str, Any]:
    return await _set_mcp_approved(request, name, False)


@router.post("/mcp_servers/{name}/refresh")
async def refresh_mcp_server(name: str, request: Request) -> dict[str, Any]:
    """Re-probe the MCP server and update its tool list in Redis.

    Called automatically by amaze-mcp-register when the server restarts
    and gets a 409 (already registered). Preserves the existing approval
    state — does NOT reset approved to true.
    """
    r = request.app.state.redis
    try:
        raw = await r.get(f"mcp:{name}")
        if not raw:
            raise HTTPException(status_code=404, detail="mcp-not-found")
        data = json.loads(raw)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("mcp_servers refresh: redis read failed for %s: %s", name, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    try:
        tool_objects = await probe_tools(data["url"])
    except ProbeError as e:
        raise HTTPException(status_code=502, detail=f"mcp-probe-failed: {e}") from e

    payload = json.dumps({"url": data["url"], "tools": tool_objects})
    try:
        await r.set(f"mcp:{name}", payload)
    except Exception as e:  # noqa: BLE001
        logger.error("mcp_servers refresh: redis write failed for %s: %s", name, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    approved_flag = await r.get(f"mcp:{name}:approved")
    approved = approved_flag != "false"
    logger.info("mcp_servers: refreshed name=%s tools=%d", name, len(tool_objects))
    return {"name": name, "url": data["url"], "tools": tool_objects, "approved": approved}
