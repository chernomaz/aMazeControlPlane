"""Live debugger endpoints — Phase 2.

Endpoints (all under /agents/{agent_id}/debug):
  PUT  /agents/{agent_id}/debug            - enable/disable debug mode
  GET  /agents/{agent_id}/debug/current    - poll current paused step + history
  POST /agents/{agent_id}/debug/next       - advance past the current step
  POST /agents/{agent_id}/debug/skip-all  - release all queued steps at once

Every debugger key carries a per-user segment ":{user}" right after the
agent id, so concurrent users debugging the same agent never collide. The
user is read from the X-Amaze-Debug-User request header; an untagged request
has no user and is rejected with 400.

Redis keyspace:
  debug:{agent_id}:{user}:enabled          STRING  "1"  (ENABLED_TTL)
  debug:{agent_id}:{user}:skip_mode        STRING  "1"  (SKIP_TTL)
  debug:{agent_id}:{user}:queue            LIST    [step_id, ...]
  debug:{agent_id}:{user}:step:{step_id}   HASH    step metadata   (STEP_TTL)
  debug:{agent_id}:{user}:gate:{step_id}   LIST    ["continue"]    (GATE_TTL)
  debug:{agent_id}:{user}:step:{step_id}:override  STRING  override_value  (OVERRIDE_TTL)

Peer-agent propagation (A2A sub-agents):
  debug:{peer_id}:{user}:primary_agent     STRING  primary_agent_id  (ENABLED_TTL)

  When debug is enabled for primary agent P, each of its allowed_agents peers
  gets `debug:{peer}:{user}:primary_agent = P`.  The proxy debug_pauser reads
  this key and routes the peer's steps into P's history/queue instead of its
  own. The primary's enabled key is refreshed by UI polling; the primary_agent
  keys on peers are refreshed at the same time so they share the same TTL.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from services.orchestrator.auth import User, current_user, require_agent_owner
from services.proxy import policy_store

logger = logging.getLogger(__name__)

router = APIRouter()

# --- TTL constants -----------------------------------------------------------

ENABLED_TTL  = 90    # seconds; refreshed by UI polling as keepalive
STEP_TTL     = 600   # seconds
SKIP_TTL     = 300   # seconds
GATE_TTL     = 30    # seconds after RPUSH (gate consumed quickly)
OVERRIDE_TTL = 300   # seconds


# --- Pydantic models ---------------------------------------------------------

class EnableDebugBody(BaseModel):
    enabled: bool


class NextStepBody(BaseModel):
    step_id: str
    override: str | None = None


# --- Helpers -----------------------------------------------------------------

# Debug-user ids are client-supplied and interpolated into Redis key names, so
# constrain them to a safe charset/length (UUIDs from the UI pass). This keeps
# the debug:* keyspace well-formed and prevents header-crafted key pollution.
_DEBUG_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _debug_user(request: Request) -> str:
    """Extract + validate the debugging user from the request headers.

    Every debugger key is scoped per-user. An untagged request (missing the
    X-Amaze-Debug-User header) has no user and is rejected with 400; a value
    that isn't a safe id (charset/length) is rejected the same way.
    """
    user = request.headers.get("x-amaze-debug-user", "").strip()
    if not user:
        raise HTTPException(status_code=400, detail="debug-user-required")
    if not _DEBUG_USER_RE.match(user):
        raise HTTPException(status_code=400, detail="debug-user-invalid")
    return user


# --- Endpoints ---------------------------------------------------------------

@router.put("/agents/{agent_id}/debug")
async def set_debug(
    agent_id: str, body: EnableDebugBody, request: Request,
    auth_user: User = Depends(current_user),
) -> dict[str, Any]:
    """Enable or disable debug (step-through) mode for an agent.

    When enabled, the proxy intercepts every call and parks it in the queue
    until the UI calls /next or /skip-all. The ENABLED_TTL acts as a
    dead-man's switch: if the UI disappears, polling stops, the key expires,
    and the proxy stops intercepting within ENABLED_TTL seconds.
    """
    r: redis.Redis = request.app.state.redis
    # S7: ownership gate. 401 (current_user dep) already ran; a non-owner gets
    # 403 / unknown agent 404 here, BEFORE the 400 missing-debug-user path — so
    # a valid X-Amaze-Debug-User UUID is no longer an authz bypass. The UUID
    # stays the concurrency key only.
    await require_agent_owner(r, agent_id, auth_user)
    user = _debug_user(request)

    # Resolve peer agents from the primary's policy so we can propagate debug.
    try:
        policy = await policy_store.get_policy(agent_id)
        peer_ids: list[str] = list(policy.allowed_agents) if policy else []
    except Exception:  # noqa: BLE001
        peer_ids = []

    try:
        if body.enabled:
            await r.setex(f"debug:{agent_id}:{user}:enabled", ENABLED_TTL, "1")
            # Reset history, queue, AND skip_mode for a fresh session — a stale
            # skip_mode (from a prior Skip All, 300 s TTL) would otherwise make
            # the proxy pass everything through and the new session never pause.
            await r.delete(
                f"debug:{agent_id}:{user}:history",
                f"debug:{agent_id}:{user}:queue",
                f"debug:{agent_id}:{user}:skip_mode",
            )
            # Propagate to peer agents so their internal calls (LLM, MCP) are
            # also intercepted and routed into the primary's history/queue.
            for peer_id in peer_ids:
                await r.setex(
                    f"debug:{peer_id}:{user}:primary_agent", ENABLED_TTL, agent_id
                )
        else:
            await r.delete(
                f"debug:{agent_id}:{user}:enabled",
                f"debug:{agent_id}:{user}:skip_mode",
            )
            # Clear peer routing keys.
            for peer_id in peer_ids:
                await r.delete(f"debug:{peer_id}:{user}:primary_agent")
    except redis.RedisError as e:
        logger.error("debugger set_debug: redis error for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    return {"agent_id": agent_id, "enabled": body.enabled}


@router.get("/agents/{agent_id}/debug/current")
async def get_current(
    agent_id: str, request: Request,
    auth_user: User = Depends(current_user),
) -> dict[str, Any]:
    """Return the current paused step and history. Polled by the UI every 1 s.

    Polling this endpoint refreshes the enabled key (keepalive). If the UI
    goes away the key expires and the proxy stops intercepting.
    """
    r: redis.Redis = request.app.state.redis
    # S7: ownership gate. 401 (current_user dep) already ran; a non-owner gets
    # 403 / unknown agent 404 here, BEFORE the 400 missing-debug-user path — so
    # a valid X-Amaze-Debug-User UUID is no longer an authz bypass. The UUID
    # stays the concurrency key only.
    await require_agent_owner(r, agent_id, auth_user)
    user = _debug_user(request)
    try:
        # Keepalive: refresh TTL on the enabled flag AND all peer routing keys
        # so that peer agents don't lose their primary_agent binding while the
        # session is active.
        enabled = await r.exists(f"debug:{agent_id}:{user}:enabled")
        if enabled:
            await r.expire(f"debug:{agent_id}:{user}:enabled", ENABLED_TTL)
            try:
                policy = await policy_store.get_policy(agent_id)
                for peer_id in (list(policy.allowed_agents) if policy else []):
                    await r.expire(f"debug:{peer_id}:{user}:primary_agent", ENABLED_TTL)
            except Exception:  # noqa: BLE001 — best effort
                pass

        # Always read the full history first — persistent list that grows as
        # steps arrive and never shrinks. This keeps the sequence diagram
        # populated even after steps are continued (removed from the queue).
        all_ids: list[str] = await r.lrange(f"debug:{agent_id}:{user}:history", 0, -1)
        history: list[dict[str, Any]] = []
        for hid in all_ids:
            entry = await r.hgetall(f"debug:{agent_id}:{user}:step:{hid}")
            if entry:
                history.append(dict(entry))

        # Peek at the head of the queue without consuming it.
        step_id: str | None = await r.lindex(f"debug:{agent_id}:{user}:queue", 0)
        if not step_id:
            return {"paused": False, "step": None, "history": history}

        # Fetch the step metadata hash.
        step_data: dict[str, str] = await r.hgetall(
            f"debug:{agent_id}:{user}:step:{step_id}"
        )
        if not step_data:
            # Step expired while it was in the queue — discard and report idle.
            await r.lpop(f"debug:{agent_id}:{user}:queue")
            return {"paused": False, "step": None, "history": history}

        return {
            "paused": step_data.get("status") == "paused",
            "step": dict(step_data),
            "history": history,
        }

    except redis.RedisError as e:
        logger.error("debugger get_current: redis error for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e


@router.post("/agents/{agent_id}/debug/next")
async def advance_next(
    agent_id: str, body: NextStepBody, request: Request,
    auth_user: User = Depends(current_user),
) -> dict[str, Any]:
    """Advance past the currently paused step.

    Validates that the UI's step_id matches the queue head (prevents
    double-advancing on stale UI state), optionally records an override value
    for the proxy to read, pops the step, and pushes the gate signal.
    """
    r: redis.Redis = request.app.state.redis
    # S7: ownership gate. 401 (current_user dep) already ran; a non-owner gets
    # 403 / unknown agent 404 here, BEFORE the 400 missing-debug-user path — so
    # a valid X-Amaze-Debug-User UUID is no longer an authz bypass. The UUID
    # stays the concurrency key only.
    await require_agent_owner(r, agent_id, auth_user)
    user = _debug_user(request)
    try:
        head: str | None = await r.lindex(f"debug:{agent_id}:{user}:queue", 0)
        if head != body.step_id:
            # Either already advanced or a different step is at the front.
            raise HTTPException(status_code=409, detail="step-already-advanced")

        if body.override is not None:
            await r.setex(
                f"debug:{agent_id}:{user}:step:{body.step_id}:override",
                OVERRIDE_TTL,
                body.override,
            )

        await r.lpop(f"debug:{agent_id}:{user}:queue")

        gate_key = f"debug:{agent_id}:{user}:gate:{body.step_id}"
        await r.rpush(gate_key, "continue")
        await r.expire(gate_key, GATE_TTL)

    except HTTPException:
        raise
    except redis.RedisError as e:
        logger.error("debugger advance_next: redis error for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    return {"advanced": body.step_id}


@router.post("/agents/{agent_id}/debug/skip-all")
async def skip_all(
    agent_id: str, request: Request,
    auth_user: User = Depends(current_user),
) -> dict[str, Any]:
    """Release all queued steps at once and engage skip-mode.

    Skip-mode tells the proxy to pass future steps through immediately
    without parking them, until the SKIP_TTL expires or debug is disabled.
    """
    r: redis.Redis = request.app.state.redis
    # S7: ownership gate. 401 (current_user dep) already ran; a non-owner gets
    # 403 / unknown agent 404 here, BEFORE the 400 missing-debug-user path — so
    # a valid X-Amaze-Debug-User UUID is no longer an authz bypass. The UUID
    # stays the concurrency key only.
    await require_agent_owner(r, agent_id, auth_user)
    user = _debug_user(request)
    count = 0
    try:
        await r.setex(f"debug:{agent_id}:{user}:skip_mode", SKIP_TTL, "1")

        while True:
            step_id: str | None = await r.lpop(f"debug:{agent_id}:{user}:queue")
            if step_id is None:
                break
            gate_key = f"debug:{agent_id}:{user}:gate:{step_id}"
            await r.rpush(gate_key, "continue")
            await r.expire(gate_key, GATE_TTL)
            count += 1

    except redis.RedisError as e:
        logger.error("debugger skip_all: redis error for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    return {"skipped": count}
