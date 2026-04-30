import json
import logging
import time

import redis.asyncio as redis
from mitmproxy import http

from services.proxy._redis import client as redis_client

logger = logging.getLogger(__name__)


def _safe_json_loads(content: bytes | str | None) -> dict:
    """Best-effort JSON decode. Returns {} on any error/empty input."""
    if not content:
        return {}
    try:
        result = json.loads(content)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


class AuditLog:
    async def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("amaze_bypass"):
            return

        agent_id = flow.metadata.get("amaze_agent", "unknown")
        session_id = flow.metadata.get("amaze_session", "")

        span = flow.metadata.get("otel_span")
        if span is not None:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else ""
            span_id = format(ctx.span_id, "016x") if ctx.span_id else ""
        else:
            trace_id = span_id = ""

        kind = flow.metadata.get("amaze_kind", "unknown")

        if kind == "mcp":
            target = flow.metadata.get("amaze_mcp_server", "")
        elif kind == "a2a":
            target = flow.metadata.get("amaze_target", "")
        elif kind == "llm":
            target = flow.metadata.get("amaze_llm_provider", "")
        else:
            # Early-deny path: PolicyEnforcer didn't set amaze_kind/target
            # before deny(). Fall back to the request host so the audit row
            # still names the destination (host-not-allowed in particular).
            target = flow.request.pretty_host or ""

        tool = flow.metadata.get("amaze_mcp_tool", "")

        raw_input = (flow.request.content or b"")[:2000].decode("utf-8", errors="replace")
        raw_output = (
            (flow.response.content or b"")[:2000].decode("utf-8", errors="replace")
            if flow.response
            else ""
        )

        denied = flow.response is not None and flow.response.status_code >= 400

        # Skip MCP protocol negotiation noise (initialize, notifications/*,
        # tools/list, resources/list, prompts/list, etc.).  Only record actual
        # tool invocations (tools/call) and denied MCP calls so the audit log
        # contains business-relevant events, not transport handshake chatter.
        if kind == "mcp" and not tool and not denied:
            return

        # Parse request + response bodies ONCE; reuse below. Saves up to 3
        # JSON decodes on every audit write (LLM responses can be sizeable).
        req_body = _safe_json_loads(flow.request.content)
        resp_body = _safe_json_loads(flow.response.content) if flow.response else {}

        # Two LLM-shape flags for the traces UI to distinguish hop kinds:
        #
        # `indirect`         — this LLM call's RESPONSE contained tool_calls
        #                      (i.e. the model is delegating to a tool, not
        #                      writing a final answer). True for the
        #                      "planner" hop in a tool-using loop.
        #
        # `has_tool_calls_input` — this LLM call's REQUEST included messages
        #                      with role="tool"/"function" (i.e. the model
        #                      was given prior tool RESULTS to synthesize
        #                      from). True for the "synthesizer" hop. We
        #                      deliberately do NOT match assistant messages
        #                      that mention tool_calls — those are message
        #                      history, not tool results being fed back.
        indirect = False
        has_tool_calls_input = False
        if kind == "llm":
            for choice in resp_body.get("choices", []) or []:
                msg = choice.get("message", {}) or {}
                if msg.get("tool_calls"):
                    indirect = True
                    break
            for m in req_body.get("messages", []) or []:
                if m.get("role") in ("tool", "function"):
                    has_tool_calls_input = True
                    break

        denial_reason = ""
        if denied:
            denial_reason = str(resp_body.get("reason", ""))

        # Per CLAUDE.md §5: an alert is written for ANY violation, regardless
        # of mode. Explicit alerts (budget/rate/graph) carry rich JSON context
        # set by the responsible addon. For other policy denials (e.g.
        # tool-not-allowed, llm-not-allowed) we synthesize a minimal alert
        # from the denial response so the alerts UI always has something to
        # display per violation.
        #
        # Use explicit `is not None` rather than truthy-or — an addon may
        # legitimately set amaze_violation = {} (empty) and we shouldn't
        # silently fall through to synthesis in that case.
        for key in ("amaze_budget_alert", "amaze_rate_alert", "amaze_violation"):
            alert_data = flow.metadata.get(key)
            if alert_data is not None:
                break
        else:
            alert_data = None

        if alert_data is None and denied and denial_reason:
            alert_data = {
                "type": denial_reason,
                "kind": kind,
                "agent_id": agent_id,
            }
            if tool:
                alert_data["tool"] = tool
            if target:
                alert_data["target"] = target
            # Pull any extra structured fields the deny envelope put in the body
            for k in ("server", "tool", "provider", "agent_id", "host",
                      "field", "current", "limit", "step_id", "expected",
                      "window"):
                if k in resp_body and k not in alert_data:
                    alert_data[k] = resp_body[k]
        alert = json.dumps(alert_data) if alert_data else ""

        record = {
            "trace_id": trace_id,
            "span_id": span_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "kind": kind,
            "target": target,
            "tool": tool,
            "input": raw_input,
            "output": raw_output,
            "ts": str(int(time.time())),
            "denied": "true" if denied else "false",
            "denial_reason": denial_reason,
            "alert": alert,
            "indirect": "true" if indirect else "false",
            "has_tool_calls_input": "true" if has_tool_calls_input else "false",
        }

        try:
            r = await redis_client()
            await r.xadd(f"audit:{agent_id}", record)
            await r.xadd("audit:global", record)
        except redis.RedisError as exc:
            logger.warning("AuditLog: redis write failed: %s", exc)


addons = [AuditLog()]
