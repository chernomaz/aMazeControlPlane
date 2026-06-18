"""
DebugPauser addon — pauses flows when debug mode is active for an agent.

Uses mitmproxy's native flow.intercept() / flow.resume() so the request hook
returns immediately — no TCP read-timeout pressure on the agent SDK. A
background asyncio task polls Redis for the user's "Next" signal.

Inserted after GraphEnforcer, before StreamBlocker in main.py.

Request interception sequence:
  1. request() hook: write step to Redis, call flow.intercept(), spawn task, return.
  2. Remaining addons (StreamBlocker, etc.) run their request hooks normally.
  3. mitmproxy sees intercepted=True → holds flow before forwarding to upstream.
  4. Background task: BLPOP in 5 s chunks; re-check enabled key each cycle.
  5. User presses Next → apply override (re-inject stream:false for LLM), resume.
  6. Browser gone → enabled key expires → cancel with 403, resume.

Response interception: same pattern; on browser-gone, resume with original response.

Redis keys:
  debug:{agent_id}:enabled             STRING  90 s   debug mode flag (keepalive by UI)
  debug:{agent_id}:skip_mode           STRING  300 s  skip-all flag
  debug:{agent_id}:step:{step_id}      HASH    600 s  step data
  debug:{agent_id}:queue               LIST    600 s  step_ids in order
  debug:{agent_id}:gate:{step_id}      LIST    30 s   BLPOP gate (short TTL — consumed fast)
  debug:{agent_id}:step:{step_id}:override  STRING  300 s  user-edited body
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from mitmproxy import http

from services.proxy._redis import client as redis_client

logger = logging.getLogger("amaze.debug_pauser")

BODY_LIMIT = 65536   # 64 KB


def _build_target(flow: http.HTTPFlow) -> str:
    """Mirror the target-resolution logic used in audit_log.py."""
    kind = flow.metadata.get("amaze_kind", "")
    if kind == "mcp":
        return flow.metadata.get("amaze_mcp_server", "") or ""
    if kind == "a2a":
        return flow.metadata.get("amaze_target", "") or ""
    if kind == "llm":
        return flow.metadata.get("amaze_llm_provider", "") or ""
    return flow.request.pretty_host or ""


async def _resolve_primary(r, agent_id: str) -> tuple[str, str, str]:
    """Return (primary_id, enabled_key, skip_key) for the given agent.

    If this agent is a peer (has debug:{agent_id}:primary_agent set), its steps
    are routed into the primary's history/queue.  The primary's enabled and
    skip_mode keys are used for all gating decisions.
    """
    raw = await r.get(f"debug:{agent_id}:primary_agent")
    if raw:
        primary_id = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    else:
        primary_id = agent_id
    return primary_id, f"debug:{primary_id}:enabled", f"debug:{primary_id}:skip_mode"


async def _poll_gate(r, gate_key: str, enabled_key: str) -> bool:
    """Poll BLPOP in 5 s chunks; re-check enabled key each iteration.

    Returns True  — gate opened (user pressed Next / Skip All).
    Returns False — debug disabled (browser gone, TTL expired).
    """
    while True:
        result = await r.blpop(gate_key, timeout=5)
        if result is not None:
            return True
        if not await r.exists(enabled_key):
            return False


class DebugPauser:
    # ── Request hook ──────────────────────────────────────────────────────────

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return  # already denied by an earlier addon

        agent_id = flow.metadata.get("amaze_agent")
        if not agent_id:
            return

        r = await redis_client()

        # Resolve the primary agent: peers route into the primary's session.
        primary_id, enabled_key, skip_key = await _resolve_primary(r, agent_id)

        if not await r.exists(enabled_key):
            return  # fast path — zero overhead when debug is off

        if await r.exists(skip_key):
            return  # skip-all was pressed — pass through immediately

        kind = flow.metadata.get("amaze_kind")
        if not kind:
            return

        tool = flow.metadata.get("amaze_mcp_tool") or ""
        # Skip MCP protocol negotiation noise (initialize, tools/list,
        # notifications/*, etc.) — same filter as audit_log.py.
        if kind == "mcp" and not tool:
            return

        step_id      = str(uuid.uuid4())
        target       = _build_target(flow)
        body         = (flow.request.content or b"")[:BODY_LIMIT].decode("utf-8", errors="replace")
        # Store step metadata and gate under the PRIMARY's namespace so the UI
        # can find them via a single history/queue key.
        step_key     = f"debug:{primary_id}:step:{step_id}"
        queue_key    = f"debug:{primary_id}:queue"
        hist_key     = f"debug:{primary_id}:history"
        gate_key     = f"debug:{primary_id}:gate:{step_id}"
        override_key = f"debug:{primary_id}:step:{step_id}:override"

        await r.hset(step_key, mapping={
            "step_id": step_id,
            "agent":   agent_id,   # which agent actually made this call
            "phase":   "request",
            "kind":    kind,
            "target":  target,
            "tool":    tool,
            "body":    body,
            "status":  "paused",
        })
        await r.expire(step_key, 600)
        # Push to history FIRST so that any UI poll that sees this step in the
        # queue also sees it in history (avoids a 1-second lag where the right
        # panel shows the paused step but the diagram is missing it).
        await r.rpush(hist_key, step_id)
        await r.expire(hist_key, 600)
        await r.rpush(queue_key, step_id)
        await r.expire(queue_key, 600)

        logger.info(
            "DebugPauser: request paused primary=%s agent=%s kind=%s step=%s",
            primary_id, agent_id, kind, step_id,
        )

        # Intercept and return immediately — mitmproxy holds the connection;
        # no TCP read-timeout fires on the agent SDK.
        flow.intercept()
        loop = asyncio.get_running_loop()
        loop.create_task(self._resume_request(
            flow, agent_id, kind,
            step_id, step_key, gate_key, override_key, enabled_key,
        ))

    async def _resume_request(
        self,
        flow: http.HTTPFlow,
        agent_id: str,
        kind: str,
        step_id: str,
        step_key: str,
        gate_key: str,
        override_key: str,
        enabled_key: str,
    ) -> None:
        try:
            r = await redis_client()
            opened = await _poll_gate(r, gate_key, enabled_key)

            if not opened:
                logger.info(
                    "DebugPauser: debug expired, cancelling request agent=%s step=%s",
                    agent_id, step_id,
                )
                await r.hset(step_key, "status", "cancelled")
                flow.response = http.Response.make(
                    403,
                    json.dumps({"error": "denied", "reason": "debug-cancelled"}),
                    {"Content-Type": "application/json"},
                )
                flow.resume()
                return

            # Apply override if user edited the body.
            override = await r.get(override_key)
            if override is not None:
                encoded = override.encode("utf-8")
                # StreamBlocker already ran and injected "stream": false into
                # flow.request.content. The override was built from the
                # pre-StreamBlocker body, so re-inject it now.
                if kind == "llm":
                    try:
                        body_dict = json.loads(encoded)
                        body_dict["stream"] = False
                        encoded = json.dumps(body_dict).encode()
                    except (json.JSONDecodeError, ValueError):
                        pass
                flow.request.content = encoded
                flow.request.headers["Content-Length"] = str(len(encoded))
                await r.delete(override_key)
                logger.info(
                    "DebugPauser: request override applied agent=%s step=%s len=%d",
                    agent_id, step_id, len(encoded),
                )

            await r.hset(step_key, "status", "continued")
            logger.info(
                "DebugPauser: request resumed agent=%s step=%s",
                agent_id, step_id,
            )

        except Exception:
            logger.exception(
                "DebugPauser: _resume_request error agent=%s step=%s",
                agent_id, step_id,
            )
        finally:
            # Always resume — even on error — to avoid hanging the flow.
            try:
                flow.resume()
            except Exception:
                pass

    # ── Response hook ─────────────────────────────────────────────────────────

    async def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None:
            return

        agent_id = flow.metadata.get("amaze_agent")
        if not agent_id:
            return

        r = await redis_client()

        primary_id, enabled_key, skip_key = await _resolve_primary(r, agent_id)

        if not await r.exists(enabled_key):
            return

        if await r.exists(skip_key):
            return

        kind = flow.metadata.get("amaze_kind")
        if not kind:
            return

        tool = flow.metadata.get("amaze_mcp_tool") or ""
        # Skip MCP protocol noise on the response side too.
        if kind == "mcp" and not tool:
            return

        # SSE responses are streamed — can't pause/edit them.
        # BUT for MCP tool calls (kind==mcp, tool set) we record the response
        # to history so the diagram shows both sides of the call.  The step is
        # NOT added to the queue so the agent continues without waiting.
        ct = flow.response.headers.get("content-type", "").lower()
        if "text/event-stream" in ct:
            if kind == "mcp" and tool:
                sse_target   = _build_target(flow)
                sse_hist_key = f"debug:{primary_id}:history"
                raw = (flow.response.content or b"")[:BODY_LIMIT].decode("utf-8", errors="replace")
                # SSE frames look like "data: <json>\n\n" — extract last data line.
                data_lines = [
                    ln[5:].strip()
                    for ln in raw.splitlines()
                    if ln.startswith("data:")
                ]
                parsed_body = data_lines[-1] if data_lines else raw
                sse_step_id  = str(uuid.uuid4())
                sse_step_key = f"debug:{primary_id}:step:{sse_step_id}"
                await r.hset(sse_step_key, mapping={
                    "step_id": sse_step_id,
                    "agent":   agent_id,
                    "phase":   "response",
                    "kind":    kind,
                    "target":  sse_target,
                    "tool":    tool,
                    "body":    parsed_body,
                    "status":  "pass_through",
                })
                await r.expire(sse_step_key, 600)
                await r.rpush(sse_hist_key, sse_step_id)
                await r.expire(sse_hist_key, 600)
                logger.info(
                    "DebugPauser: SSE mcp response recorded (not paused) primary=%s agent=%s step=%s",
                    primary_id, agent_id, sse_step_id,
                )
            return

        step_id      = str(uuid.uuid4())
        target       = _build_target(flow)
        body         = (flow.response.content or b"")[:BODY_LIMIT].decode("utf-8", errors="replace")
        step_key     = f"debug:{primary_id}:step:{step_id}"
        queue_key    = f"debug:{primary_id}:queue"
        hist_key     = f"debug:{primary_id}:history"
        gate_key     = f"debug:{primary_id}:gate:{step_id}"
        override_key = f"debug:{primary_id}:step:{step_id}:override"

        await r.hset(step_key, mapping={
            "step_id": step_id,
            "agent":   agent_id,
            "phase":   "response",
            "kind":    kind,
            "target":  target,
            "tool":    tool,
            "body":    body,
            "status":  "paused",
        })
        await r.expire(step_key, 600)
        # History before queue — same ordering guarantee as the request hook.
        await r.rpush(hist_key, step_id)
        await r.expire(hist_key, 600)
        await r.rpush(queue_key, step_id)
        await r.expire(queue_key, 600)

        logger.info(
            "DebugPauser: response paused primary=%s agent=%s kind=%s step=%s",
            primary_id, agent_id, kind, step_id,
        )

        flow.intercept()
        loop = asyncio.get_running_loop()
        loop.create_task(self._resume_response(
            flow, agent_id,
            step_id, step_key, gate_key, override_key, enabled_key,
        ))

    async def _resume_response(
        self,
        flow: http.HTTPFlow,
        agent_id: str,
        step_id: str,
        step_key: str,
        gate_key: str,
        override_key: str,
        enabled_key: str,
    ) -> None:
        try:
            r = await redis_client()
            opened = await _poll_gate(r, gate_key, enabled_key)

            if not opened:
                logger.info(
                    "DebugPauser: debug expired, passing response through agent=%s step=%s",
                    agent_id, step_id,
                )
                await r.hset(step_key, "status", "cancelled")
                # Cannot deny on response side — upstream already answered.
                # Resume passes the original response through unchanged.
                flow.resume()
                return

            override = await r.get(override_key)
            if override is not None and flow.response is not None:
                encoded = override.encode("utf-8")
                flow.response.content = encoded
                flow.response.headers["Content-Length"] = str(len(encoded))
                await r.delete(override_key)
                logger.info(
                    "DebugPauser: response override applied agent=%s step=%s len=%d",
                    agent_id, step_id, len(encoded),
                )

            await r.hset(step_key, "status", "continued")
            logger.info(
                "DebugPauser: response resumed agent=%s step=%s",
                agent_id, step_id,
            )

        except Exception:
            logger.exception(
                "DebugPauser: _resume_response error agent=%s step=%s",
                agent_id, step_id,
            )
        finally:
            try:
                flow.resume()
            except Exception:
                pass
