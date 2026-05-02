import asyncio
import json
import logging
import time

import redis.asyncio as redis
from mitmproxy import http

from services.proxy._redis import client as redis_client

logger = logging.getLogger(__name__)

MCP_SESSION_HEADER = "mcp-session-id"
MCP_PENDING_TTL = 120  # seconds; covers slow tools


def _safe_json_loads(content: bytes | str | None) -> dict:
    """Best-effort JSON decode. Returns {} on any error/empty input."""
    if not content:
        return {}
    try:
        result = json.loads(content)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _extract_sse_body(content: bytes) -> str:
    """Extract JSON payload from SSE bytes (used for buffered POST SSE responses)."""
    text = content.decode("utf-8", errors="replace")
    data_lines = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].lstrip(" ")
            if payload and payload != "[DONE]":
                data_lines.append(payload)
    if data_lines:
        return data_lines[-1] if len(data_lines) == 1 else "\n".join(data_lines)
    return text


async def _write_mcp_result(mcp_session_id: str, jsonrpc_id: object, payload: str) -> None:
    """Look up a pending MCP tool call by session+id and write the complete audit entry."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return

    # Only JSON-RPC responses carry both "id" and "result"/"error".
    if "id" not in data or ("result" not in data and "error" not in data):
        return

    redis_key = f"mcp_pending:{mcp_session_id}:{data['id']}"
    try:
        r = await redis_client()
        pending_raw = await r.get(redis_key)
        if not pending_raw:
            return  # no matching tool call (protocol noise, or TTL expired)

        await r.delete(redis_key)
        pending = json.loads(pending_raw)

        result_obj = data.get("result", data.get("error", {}))
        output = json.dumps(result_obj)[:8000]

        record = {
            "trace_id":             pending["trace_id"],
            "span_id":              pending["span_id"],
            "agent_id":             pending["agent_id"],
            "session_id":           pending["session_id"],
            "kind":                 "mcp",
            "target":               pending["target"],
            "tool":                 pending["tool"],
            "input":                pending["input"],
            "output":               output,
            "ts":                   pending["ts"],
            "denied":               "false",
            "denial_reason":        "",
            "alert":                "",
            "indirect":             "false",
            "has_tool_calls_input": "false",
        }

        await r.xadd(f"audit:{pending['agent_id']}", record)
        await r.xadd("audit:global", record)
    except (redis.RedisError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("AuditLog: MCP result write failed: %s", exc)


async def _write_audit_record(agent_id: str, record: dict) -> None:
    """Write a complete audit record to both per-agent and global streams."""
    try:
        r = await redis_client()
        await r.xadd(f"audit:{agent_id}", record)
        await r.xadd("audit:global", record)
    except redis.RedisError as exc:
        logger.warning("AuditLog: redis write failed: %s", exc)


class AuditLog:
    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("amaze_bypass"):
            return
        kind = flow.metadata.get("amaze_kind")
        tool = flow.metadata.get("amaze_mcp_tool", "")
        if kind != "mcp" or not tool:
            return
        mcp_session_id = flow.request.headers.get(MCP_SESSION_HEADER, "")
        if not mcp_session_id:
            return
        req_body = _safe_json_loads(flow.request.content)
        jsonrpc_id = req_body.get("id")
        if jsonrpc_id is None:
            return
        span = flow.metadata.get("otel_span")
        if span is not None:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else ""
            span_id  = format(ctx.span_id,  "016x") if ctx.span_id  else ""
        else:
            trace_id = span_id = ""
        agent_id   = flow.metadata.get("amaze_agent", "unknown")
        session_id = flow.metadata.get("amaze_session", "")
        target     = flow.metadata.get("amaze_mcp_server", "")
        raw_input  = (flow.request.content or b"")[:8000].decode("utf-8", errors="replace")
        pending = {
            "trace_id":   trace_id,
            "span_id":    span_id,
            "agent_id":   agent_id,
            "session_id": session_id,
            "target":     target,
            "tool":       tool,
            "input":      raw_input,
            "ts":         str(int(time.time())),
        }
        redis_key = f"mcp_pending:{mcp_session_id}:{jsonrpc_id}"
        try:
            r = await redis_client()
            await r.setex(redis_key, MCP_PENDING_TTL, json.dumps(pending))
            flow.metadata["amaze_mcp_pending_key"] = redis_key
        except redis.RedisError as exc:
            logger.warning("AuditLog: request-time pending store failed: %s", exc)

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Route SSE responses to the appropriate streaming handler."""
        if not flow.response:
            return
        ct = flow.response.headers.get("content-type", "").lower()
        if "text/event-stream" not in ct:
            return

        if flow.request.method == "GET":
            # GET SSE: long-lived stream carrying async tool results.
            # Accumulate chunks, parse frames, correlate via Redis pending key.
            flow.response.stream = self._make_get_sse_interceptor(flow)
        else:
            # POST SSE: server returned the tool result inline (200 OK) in the
            # POST response body, as one or more SSE frames.  We cannot use
            # stream=False here because the server uses chunked + keep-alive —
            # mitmproxy would wait for the connection to close (never happens)
            # and flow.response.content would be empty.  Use a streaming
            # interceptor instead: accumulate chunks, parse frames, write
            # the audit record as soon as the complete frame arrives.
            kind = flow.metadata.get("amaze_kind")
            tool = flow.metadata.get("amaze_mcp_tool", "")
            if kind == "mcp" and tool and flow.response.status_code == 200:
                flow.response.stream = self._make_post_sse_record_writer(flow)
            else:
                flow.response.stream = False

    def _make_get_sse_interceptor(self, flow: http.HTTPFlow):
        """Streaming callback for GET SSE: correlates result frames with pending POST calls."""
        mcp_session_id = flow.request.headers.get(MCP_SESSION_HEADER, "")
        buf = bytearray()

        def handle(chunk: bytes) -> bytes:
            buf.extend(chunk)
            sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
            while sep in buf:
                idx = buf.index(sep)
                frame = buf[:idx].decode("utf-8", errors="replace")
                del buf[:idx + len(sep)]

                for line in frame.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].lstrip(" ")
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        data = json.loads(payload)
                        if "id" in data and ("result" in data or "error" in data):
                            logger.info("AuditLog: GET SSE result frame id=%s session=%s",
                                        data["id"], mcp_session_id[:8] if mcp_session_id else "")
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                _write_mcp_result(mcp_session_id, data["id"], payload)
                            )
                    except (ValueError, TypeError, RuntimeError) as exc:
                        logger.warning("AuditLog: GET SSE frame error: %s", exc)
            return chunk

        return handle

    def _make_post_sse_record_writer(self, flow: http.HTTPFlow):
        """Streaming callback for POST SSE tool results (200 OK inline response).

        Accumulates chunks until complete SSE frames arrive, then writes the
        audit record directly — no Redis pending key needed since all metadata
        is already known from the POST request.
        """
        agent_id  = flow.metadata.get("amaze_agent", "unknown")
        session_id = flow.metadata.get("amaze_session", "")
        tool      = flow.metadata.get("amaze_mcp_tool", "")
        target    = flow.metadata.get("amaze_mcp_server", "")

        span = flow.metadata.get("otel_span")
        if span is not None:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else ""
            span_id  = format(ctx.span_id,  "016x") if ctx.span_id  else ""
        else:
            trace_id = span_id = ""

        raw_input = (flow.request.content or b"")[:8000].decode("utf-8", errors="replace")
        ts = str(int(time.time()))
        buf = bytearray()

        def handle(chunk: bytes) -> bytes:
            buf.extend(chunk)
            # Support both LF-only (\n\n) and CRLF (\r\n\r\n) SSE frame delimiters.
            sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
            while sep in buf:
                idx = buf.index(sep)
                frame = buf[:idx].decode("utf-8", errors="replace")
                del buf[:idx + len(sep)]

                for line in frame.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].lstrip(" ")
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        data = json.loads(payload)
                        if "id" not in data or ("result" not in data and "error" not in data):
                            continue
                        result_obj = data.get("result", data.get("error", {}))
                        output = json.dumps(result_obj)[:8000]
                        record = {
                            "trace_id":             trace_id,
                            "span_id":              span_id,
                            "agent_id":             agent_id,
                            "session_id":           session_id,
                            "kind":                 "mcp",
                            "target":               target,
                            "tool":                 tool,
                            "input":                raw_input,
                            "output":               output,
                            "ts":                   ts,
                            "denied":               "false",
                            "denial_reason":        "",
                            "alert":                "",
                            "indirect":             "false",
                            "has_tool_calls_input": "false",
                        }
                        logger.info("AuditLog: writing MCP record tool=%s trace=%s", tool, trace_id)
                        loop = asyncio.get_running_loop()
                        loop.create_task(_write_audit_record(agent_id, record))
                    except (ValueError, TypeError, RuntimeError) as exc:
                        logger.warning("AuditLog: POST SSE record write error: %s", exc)
            return chunk

        return handle

    async def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("amaze_bypass"):
            return

        agent_id  = flow.metadata.get("amaze_agent", "unknown")
        session_id = flow.metadata.get("amaze_session", "")

        span = flow.metadata.get("otel_span")
        if span is not None:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else ""
            span_id  = format(ctx.span_id,  "016x") if ctx.span_id  else ""
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
            target = flow.request.pretty_host or ""

        tool = flow.metadata.get("amaze_mcp_tool", "")

        req_body  = _safe_json_loads(flow.request.content)
        resp_body = _safe_json_loads(flow.response.content) if flow.response else {}

        raw_input = (flow.request.content or b"")[:8000].decode("utf-8", errors="replace")
        if flow.response:
            resp_content = flow.response.content or b""
            resp_ct = flow.response.headers.get("content-type", "").lower()
            if "text/event-stream" in resp_ct and resp_content:
                raw_output = _extract_sse_body(resp_content)[:8000]
            else:
                raw_output = resp_content[:8000].decode("utf-8", errors="replace")
        else:
            raw_output = ""

        denied = flow.response is not None and flow.response.status_code >= 400

        # Skip MCP protocol negotiation noise (initialize, notifications/*, tools/list, etc.).
        if kind == "mcp" and not tool and not denied:
            return

        if kind == "mcp" and tool and not denied:
            resp_status = flow.response.status_code if flow.response else 0

            if resp_status == 200:
                # Inline SSE result: the streaming callback already wrote the record.
                # Delete the pre-stored pending key so the GET SSE path cannot write
                # a duplicate if the server echoes the same id on that stream.
                pending_key = flow.metadata.get("amaze_mcp_pending_key")
                if pending_key:
                    try:
                        r = await redis_client()
                        await r.delete(pending_key)
                    except redis.RedisError:
                        pass  # TTL will expire it within MCP_PENDING_TTL seconds
                return

            if resp_status == 202:
                # Pending key was stored at request time; GET SSE interceptor will
                # consume it when the result frame arrives.
                if flow.metadata.get("amaze_mcp_pending_key"):
                    return
                # Request-time store failed (Redis was down at request time). Retry.
                mcp_session_id = flow.request.headers.get(MCP_SESSION_HEADER, "")
                jsonrpc_id = req_body.get("id")
                if mcp_session_id and jsonrpc_id is not None:
                    pending = {
                        "trace_id":   trace_id,
                        "span_id":    span_id,
                        "agent_id":   agent_id,
                        "session_id": session_id,
                        "target":     target,
                        "tool":       tool,
                        "input":      raw_input,
                        "ts":         str(int(time.time())),
                    }
                    try:
                        r = await redis_client()
                        await r.setex(
                            f"mcp_pending:{mcp_session_id}:{jsonrpc_id}",
                            MCP_PENDING_TTL,
                            json.dumps(pending),
                        )
                        return
                    except redis.RedisError as exc:
                        logger.warning("AuditLog: pending store failed, falling back to empty entry: %s", exc)
                # Fall through: write entry with empty output if Redis is unavailable

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

        for key in ("amaze_budget_alert", "amaze_rate_alert", "amaze_violation"):
            alert_data = flow.metadata.get(key)
            if alert_data is not None:
                break
        else:
            alert_data = None

        if alert_data is None and denied and denial_reason:
            alert_data = {
                "type":     denial_reason,
                "kind":     kind,
                "agent_id": agent_id,
            }
            if tool:
                alert_data["tool"] = tool
            if target:
                alert_data["target"] = target
            for k in ("server", "tool", "provider", "agent_id", "host",
                      "field", "current", "limit", "step_id", "expected", "window"):
                if k in resp_body and k not in alert_data:
                    alert_data[k] = resp_body[k]
        alert = json.dumps(alert_data) if alert_data else ""

        record = {
            "trace_id":             trace_id,
            "span_id":              span_id,
            "agent_id":             agent_id,
            "session_id":           session_id,
            "kind":                 kind,
            "target":               target,
            "tool":                 tool,
            "input":                raw_input,
            "output":               raw_output,
            "ts":                   str(int(time.time())),
            "denied":               "true" if denied else "false",
            "denial_reason":        denial_reason,
            "alert":                alert,
            "indirect":             "true" if indirect else "false",
            "has_tool_calls_input": "true" if has_tool_calls_input else "false",
        }

        try:
            r = await redis_client()
            await r.xadd(f"audit:{agent_id}", record)
            await r.xadd("audit:global", record)
        except redis.RedisError as exc:
            logger.warning("AuditLog: redis write failed: %s", exc)


addons = [AuditLog()]
