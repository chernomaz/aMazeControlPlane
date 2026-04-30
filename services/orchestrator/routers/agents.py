"""GET /agents + agent mutation endpoints.

State classification (S4-T1.2):
  pending              - Redis `agent:{id}:approved` exists AND value == "false"
  approved-with-policy - Redis `policy:{id}` exists OR YAML policies.yaml has
                          an entry for the agent_id
  approved-no-policy   - registered but no policy entry anywhere

Mutations:
  POST /agents/{id}/approve   - write `agent:{id}:approved` = "true"   (S4-T2.1)
  POST /agents/{id}/reject    - write `agent:{id}:approved` = "false"  (S4-T2.1)
  POST /agents/{id}/messages  - forward user prompt to chat port       (S4-T3.3)

Send-message specifics (S4-T3.3):
 - Resets the agent's `trace_context:{sid}` before forwarding so each
   user-driven message gets its own trace_id (sessions otherwise persist
   one trace_id for the agent's lifetime - a CLAUDE.md §6 design choice
   that's the wrong UX for a click-to-trace demo).
 - After the agent replies, scans `audit:global` for denials within this
   call's time window and returns a `denial` field with a human-readable
   reason. If the agent's reply text matches a generic TaskGroup error,
   we replace it with the humanised denial so the user sees something
   actionable instead of "unhandled errors in a TaskGroup".
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.proxy import policy_store

logger = logging.getLogger(__name__)

router = APIRouter()

CONFIG_DIR = pathlib.Path(os.environ.get("CONFIG_DIR", "/app/config"))


def _load_policy_map() -> dict[str, dict[str, Any]]:
    """Read policies.yaml; return {agent_id: policy_dict}. {} on any error.

    Errors are swallowed deliberately — a malformed YAML must not 500 the
    list endpoint; the GUI just sees no YAML-side policies.
    """
    path = CONFIG_DIR / "policies.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        logger.warning("agents: policies.yaml unreadable: %s", e)
        return {}
    policies = data.get("policies") or {}
    return policies if isinstance(policies, dict) else {}


def _summarize_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Compact view shown in the agents list — full policy is fetched on demand."""
    return {
        "mode": policy.get("mode", "flexible"),
        "max_tokens_per_turn": policy.get("max_tokens_per_turn", 0),
        "max_tool_calls_per_turn": policy.get("max_tool_calls_per_turn", 0),
        "max_agent_calls_per_turn": policy.get("max_agent_calls_per_turn", 0),
        "allowed_llm_providers": policy.get("allowed_llm_providers", []),
        "allowed_tools": policy.get("allowed_tools", []),
        "allowed_agents": policy.get("allowed_agents", []),
    }


@router.get("/agents")
async def list_agents(request: Request) -> list[dict[str, Any]]:
    r = request.app.state.redis
    yaml_policies = _load_policy_map()

    # 1. Collect agent_ids registered in Redis (have an endpoint key).
    registered: dict[str, str] = {}  # agent_id -> endpoint
    try:
        async for key in r.scan_iter(match="agent:*:endpoint"):
            # Strip "agent:" prefix and ":endpoint" suffix.
            if not key.startswith("agent:") or not key.endswith(":endpoint"):
                continue
            agent_id = key[len("agent:"):-len(":endpoint")]
            endpoint = await r.get(key)
            if endpoint:
                registered[agent_id] = endpoint
    except Exception as e:  # noqa: BLE001 — return what we have, don't 500
        logger.error("agents: redis scan failed: %s", e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    # 2. Union with YAML-declared agent_ids.
    all_ids = set(registered) | set(yaml_policies)

    out: list[dict[str, Any]] = []
    for agent_id in sorted(all_ids):
        # State classification — pending check first (explicit gate).
        approved_flag = None
        try:
            approved_flag = await r.get(f"agent:{agent_id}:approved")
        except Exception:  # noqa: BLE001
            approved_flag = None

        # Policy presence: Redis (Phase 2 will populate) OR YAML (today).
        has_redis_policy = False
        try:
            has_redis_policy = bool(await r.exists(f"policy:{agent_id}"))
        except Exception:  # noqa: BLE001
            has_redis_policy = False
        has_yaml_policy = agent_id in yaml_policies
        has_policy = has_redis_policy or has_yaml_policy

        if approved_flag == "false":
            state = "pending"
        elif has_policy:
            state = "approved-with-policy"
        else:
            state = "approved-no-policy"

        # registered_at — best effort; we don't currently persist a timestamp,
        # so report endpoint-key TTL remaining as a proxy ("seconds_left").
        # GUI can render this as relative; absent for YAML-only entries.
        registered_at: float | None = None

        entry: dict[str, Any] = {
            "agent_id": agent_id,
            "state": state,
            "registered_at": registered_at,
        }
        if agent_id in registered:
            entry["endpoint"] = registered[agent_id]
        if has_yaml_policy:
            entry["policy_summary"] = _summarize_policy(yaml_policies[agent_id])
        out.append(entry)

    return out


# --- Approve / reject (S4-T2.1) ------------------------------------------

async def _agent_exists(r: Any, agent_id: str, yaml_policies: dict[str, Any]) -> bool:
    """An agent is 'known' if it is registered (has an endpoint key) OR
    has a policy entry in YAML. Mirrors the listing logic so the GUI can
    approve YAML-declared agents before they ever register."""
    if agent_id in yaml_policies:
        return True
    return bool(await r.exists(f"agent:{agent_id}:endpoint"))


async def _set_agent_approved(request: Request, agent_id: str, approved: bool) -> dict[str, Any]:
    r = request.app.state.redis
    yaml_policies = _load_policy_map()
    try:
        if not await _agent_exists(r, agent_id, yaml_policies):
            raise HTTPException(status_code=404, detail="agent-not-found")
        await r.set(f"agent:{agent_id}:approved", "true" if approved else "false")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — fail closed on Redis errors
        logger.error("agents: redis write failed for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    return {"agent_id": agent_id, "approved": approved}


@router.post("/agents/{agent_id}/approve")
async def approve_agent(agent_id: str, request: Request) -> dict[str, Any]:
    return await _set_agent_approved(request, agent_id, True)


@router.post("/agents/{agent_id}/reject")
async def reject_agent(agent_id: str, request: Request) -> dict[str, Any]:
    return await _set_agent_approved(request, agent_id, False)


# --- Send message (S4-T3.3) ----------------------------------------------

class SendMessageRequest(BaseModel):
    """Body shape for POST /agents/{agent_id}/messages.

    Just a non-empty user prompt. The orchestrator does no policy decisions
    here — it forwards to the agent's inbound `/chat` endpoint, exactly
    like an external user would. Real enforcement happens when the agent
    makes its OWN outbound calls through the proxy.
    """

    prompt: str = Field(min_length=1)


async def _latest_trace_id(r: Any, agent_id: str) -> str | None:
    """Fish out the trace_id of the agent's most recent audit record.

    Best-effort. Empty stream, missing field, or any Redis error → None.
    The audit_log addon writes one record per proxied call to
    `audit:{agent_id}` (see services/proxy/audit_log.py:163), each carrying
    a `trace_id` field set from the OpenTelemetry span context. We grab
    the newest entry via XREVRANGE limit=1.
    """
    try:
        entries = await r.xrevrange(f"audit:{agent_id}", count=1)
    except Exception as e:  # noqa: BLE001 — never fail the user request on this
        logger.warning("send_message: xrevrange failed for %s: %s", agent_id, e)
        return None
    if not entries:
        return None
    # entries: [(stream_id, {field: value, ...})]
    _stream_id, fields = entries[0]
    trace_id = fields.get("trace_id") if isinstance(fields, dict) else None
    return trace_id or None


def _humanize_denial(denial_reason: str, alert: dict) -> str:
    """Translate a proxy denial envelope to an operator-friendly sentence.

    The proxy emits structured `alert` JSON alongside `denial_reason` for every
    blocked call (see services/proxy/audit_log.py and §9 of CLAUDE.md). The
    LangChain layer wrapping the agent's LLM/MCP calls swallows the structured
    body and surfaces only a generic "unhandled errors in a TaskGroup" — so we
    re-derive a useful message from the audit stream.
    """
    tool = alert.get("tool")
    target = alert.get("target")
    server = alert.get("server")
    reason = denial_reason or alert.get("type", "unknown")

    if reason == "tool-not-allowed":
        srv = f" on '{server}'" if server else ""
        return f"Tool '{tool}'{srv} is not in this agent's policy.allowed_tools"
    if reason == "agent-not-allowed":
        return f"A2A call to agent '{target}' is not in this agent's policy.allowed_agents"
    if reason == "llm-not-allowed":
        provider = alert.get("provider", target or "?")
        model = alert.get("model", "")
        return f"LLM '{provider}/{model}' is not in this agent's policy.allowed_llm_providers"
    if reason == "graph_violation":
        expected = alert.get("expected") or alert.get("expected_callee")
        got = alert.get("callee_id") or tool or target or "?"
        if expected:
            return f"Graph violation: expected '{expected}' but got '{got}'"
        return f"Graph violation on call to '{got}'"
    if reason == "edge_loop_exceeded":
        callee = alert.get("callee_id") or tool or "?"
        return f"Loop limit exceeded for step '{callee}' (max_loops reached)"
    if reason == "budget_exceeded":
        kind = alert.get("limit") or alert.get("limit_type", "per-turn")
        return f"Budget exceeded: {kind} limit hit"
    if reason == "rate-limit-exceeded":
        cur = alert.get("current", "?")
        limit = alert.get("limit", "?")
        window = alert.get("window", "?")
        return f"Rate limit exceeded: {cur} of {limit} tokens in {window} window"
    if reason == "invalid-bearer":
        return "Invalid or missing bearer token"
    if reason == "mcp-not-allowed":
        return f"MCP server '{server or target}' is not in this agent's policy"
    if reason == "host-not-allowed":
        return f"Host '{target}' is not allowed by this agent's policy"
    if reason == "policy-not-found":
        return f"No policy found for agent '{alert.get('agent_id', '?')}'"
    if reason == "redis-unavailable":
        return "Internal: enforcement layer Redis unreachable (request denied fail-closed)"
    return f"{reason}"


async def _scan_recent_denials(
    r: Any, agent_id: str, since_ts: float
) -> list[dict[str, Any]]:
    """Return denial records (with parsed alert) emitted since `since_ts`.

    Scans `audit:global` so we catch denials on A2A peers too (e.g. agent-sdk
    forwards to agent-sdk2 → agent-sdk2's tool call gets denied → record lands
    on agent-sdk2's stream and on global). Newest-first.
    """
    try:
        # Walk newest-first up to ~100 records or until we cross since_ts.
        entries = await r.xrevrange("audit:global", count=100)
    except Exception as e:  # noqa: BLE001
        logger.warning("send_message: scan_recent_denials failed: %s", e)
        return []

    denials: list[dict[str, Any]] = []
    for _stream_id, fields in entries:
        if not isinstance(fields, dict):
            continue
        try:
            ts = float(fields.get("ts", "0"))
        except ValueError:
            continue
        if ts < since_ts:
            break  # stream is time-ordered; we're past the window
        if fields.get("denied") != "true":
            continue
        # Optional agent_id filter — include the calling agent and any peer
        # it might have A2A'd to in this conversation. For Phase 3 we keep
        # it simple: any denial within the window is ours, since send-message
        # is a single user request.
        record_agent = fields.get("agent_id", "")
        alert_raw = fields.get("alert", "") or "{}"
        try:
            alert_obj = json.loads(alert_raw) if alert_raw else {}
        except json.JSONDecodeError:
            alert_obj = {}
        denials.append(
            {
                "agent_id": record_agent,
                "denial_reason": fields.get("denial_reason", ""),
                "alert": alert_obj,
                "ts": ts,
            }
        )

    return denials


_TASKGROUP_SIGNATURES = (
    "unhandled errors in a TaskGroup",
    "TaskGroup",
    "ExceptionGroup",
)


def _looks_like_taskgroup_error(reply: Any) -> bool:
    """True if the agent returned a generic LangChain/asyncio wrapper error
    that hides the actual proxy denial. We rewrite these to humanised text.
    """
    if isinstance(reply, dict):
        text = str(reply.get("reply") or reply.get("text") or reply.get("error", ""))
    else:
        text = str(reply)
    return any(sig in text for sig in _TASKGROUP_SIGNATURES)


@router.post("/agents/{agent_id}/messages")
async def send_message(
    agent_id: str, body: SendMessageRequest, request: Request
) -> dict[str, Any]:
    """Forward a user prompt to the agent's inbound `/chat` endpoint.

    The orchestrator is acting as a plain HTTP client here — NOT as a
    peer agent. No bearer is minted; no proxy hop is involved. The agent's
    own internal calls (LLM/MCP/A2A) go through the proxy with its own
    bearer, which is what the trace_id will refer to.
    """
    r = request.app.state.redis
    try:
        # Prefer the chat-port URL if registered. The SDK serves user prompts
        # on a separate port (default 8080) from the A2A ingress (default
        # 9002) — `agent:{id}:endpoint` is the A2A URL. The chat URL is
        # registered under `agent:{id}:chat_endpoint` (see SDK register).
        chat_endpoint = await r.get(f"agent:{agent_id}:chat_endpoint")
        a2a_endpoint = await r.get(f"agent:{agent_id}:endpoint")
    except Exception as e:  # noqa: BLE001 — fail-closed on Redis errors
        logger.error("send_message: redis read failed for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    if not chat_endpoint and not a2a_endpoint:
        raise HTTPException(status_code=404, detail="agent-not-registered")
    if not chat_endpoint:
        raise HTTPException(status_code=409, detail="agent-chat-port-unregistered")

    # SSRF guard: reject anything that isn't an http(s) URL with a real host.
    # `chat_endpoint` is read from Redis (set manually today; auto by the SDK
    # in a follow-up). A bad value would otherwise let this endpoint pivot
    # to internal services (169.254.169.254, redis:6379, admin panels).
    parsed = urlparse(chat_endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        logger.error(
            "send_message: invalid chat_endpoint for %s: %r",
            agent_id, chat_endpoint,
        )
        raise HTTPException(status_code=409, detail="agent-chat-port-invalid")

    # Reset per-turn state at the start of every message — scoped to THIS
    # conversation's agents.
    #
    # A "turn" = one user message + the agent's full response cycle, which
    # may fan out to A2A peers (declared in the primary's policy.allowed_agents).
    # We clear:
    #   * trace_context for the primary (each user message gets a fresh trace_id)
    #   * per-turn counters + graph pointers for the primary AND every peer
    #     the primary is allowed to call (via agent_session:{peer} → sid)
    # Anything outside that set is another conversation's state — leave it
    # alone so concurrent send-messages don't wipe each other.
    try:
        # Resolve session_ids for the primary + each declared A2A peer.
        sid_keys: set[str] = set()
        primary_sid = await r.get(f"agent_session:{agent_id}")
        if primary_sid:
            sid_keys.add(primary_sid)
        try:
            policy = await policy_store.get_policy(agent_id)
        except Exception as e:  # noqa: BLE001 — best effort; missing policy ⇒ primary-only
            logger.debug("send_message: policy fetch for peers failed: %s", e)
            policy = None
        peer_ids: list[str] = list(policy.allowed_agents) if policy else []
        for peer in peer_ids:
            peer_sid = await r.get(f"agent_session:{peer}")
            if peer_sid:
                sid_keys.add(peer_sid)

        keys: list[str] = []
        for sid in sid_keys:
            keys.append(f"trace_context:{sid}")
            keys.append(f"session:{sid}:total_tokens")
            keys.append(f"session:{sid}:total_tool_calls")
            keys.append(f"session:{sid}:total_agent_calls")
            async for k in r.scan_iter(match=f"graph:{sid}:*"):
                keys.append(k)
        if keys:
            await r.delete(*keys)
    except Exception as e:  # noqa: BLE001 — best effort; not worth failing the user request
        logger.warning("send_message: turn reset failed for %s: %s", agent_id, e)

    since_ts = time.time()

    url = f"{chat_endpoint.rstrip('/')}/chat"
    # The SDK's chat handler reads `message` from the body (see
    # sdk/amaze/_a2a.py:156); pass the user prompt verbatim under that key.
    payload = {"message": body.prompt}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
    except httpx.TimeoutException as e:
        logger.warning("send_message: %s timeout to %s: %s", agent_id, url, e)
        raise HTTPException(status_code=504, detail="agent-timeout") from e
    except httpx.RequestError as e:
        logger.warning("send_message: %s unreachable at %s: %s", agent_id, url, e)
        raise HTTPException(status_code=502, detail="agent-unreachable") from e

    # Pass through the agent's body verbatim. JSON → as-is; non-JSON → wrap.
    try:
        agent_body: Any = resp.json()
    except ValueError:
        agent_body = {"text": resp.text}

    # Surface any proxy denial that fired during this message's lifetime.
    denials = await _scan_recent_denials(r, agent_id, since_ts)
    denial_payload: dict[str, Any] | None = None
    if denials:
        # Most recent denial is first (xrevrange newest-first).
        d = denials[0]
        denial_payload = {
            "reason": d["denial_reason"],
            "agent_id": d["agent_id"],
            "alert": d["alert"],
            "human": _humanize_denial(d["denial_reason"], d["alert"]),
            "count": len(denials),
        }
        # If the agent's reply is a generic TaskGroup wrapper that hides the
        # real cause, replace it with the humanised denial so the user sees
        # something actionable.
        if _looks_like_taskgroup_error(agent_body) and isinstance(agent_body, dict):
            agent_body = {
                **agent_body,
                "reply": f"Denied: {denial_payload['human']}",
            }

    trace_id = await _latest_trace_id(r, agent_id)
    return {
        "response": agent_body,
        "trace_id": trace_id,
        "denial": denial_payload,
    }


# --- Per-agent stats (S4-T4.2) -------------------------------------------

# Range tokens accepted by the dashboard endpoint. Kept as a small explicit
# map (rather than a regex + arithmetic) so the set of allowed windows is
# obvious from the source and matches the GUI's range selector exactly.
_RANGE_SECONDS: dict[str, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}


@router.get("/agents/{agent_id}/stats")
async def agent_stats(
    agent_id: str, request: Request, range: str = "24h"
) -> dict[str, Any]:
    """Dashboard payload for one agent over a fixed time window.

    Thin wrapper over `services.orchestrator.stats.get_dashboard_data`
    (T4.1). This endpoint only validates the range token, confirms the
    agent is known, and maps Redis errors to 503; the heavy lifting
    (TimeSeries reads, audit aggregation, denial breakdown) lives in
    the stats module so the GUI can call one URL per refresh.
    """
    range_seconds = _RANGE_SECONDS.get(range)
    if range_seconds is None:
        raise HTTPException(
            status_code=400,
            detail=f"invalid-range; expected one of {sorted(_RANGE_SECONDS)}",
        )

    r = request.app.state.redis
    yaml_policies = _load_policy_map()
    try:
        if not await _agent_exists(r, agent_id, yaml_policies):
            raise HTTPException(status_code=404, detail="agent-not-found")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — fail closed on Redis errors
        logger.error("agent_stats: redis read failed for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    # Imported lazily so the router still loads if T4.1's module hasn't
    # shipped yet — the endpoint just 503s instead of breaking startup.
    try:
        from services.orchestrator.stats import get_dashboard_data
    except ImportError as e:
        logger.error("agent_stats: stats module unavailable: %s", e)
        raise HTTPException(status_code=503, detail="stats-unavailable") from e

    try:
        payload = await get_dashboard_data(agent_id, range_seconds)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — Redis / TimeSeries hiccup
        logger.error("agent_stats: get_dashboard_data failed for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    # Pydantic model → dict (FastAPI handles either, but we may receive a
    # plain dict if T4.1's DashboardPayload is a TypedDict).
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if hasattr(payload, "dict"):
        return payload.dict()
    return payload  # type: ignore[return-value]
