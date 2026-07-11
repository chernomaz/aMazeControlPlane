import asyncio
import json
import logging
import time

import redis.asyncio as redis
from mitmproxy import http

from services.proxy._redis import client as redis_client
from services.proxy.pii_engine import safe_redact_json as _safe_redact_json

logger = logging.getLogger(__name__)

MCP_SESSION_HEADER = "mcp-session-id"
MCP_PENDING_TTL = 120  # seconds; covers slow tools

# In-process cache of PII output entities per pending MCP tool call, keyed by
# (mcp_session_id, jsonrpc_id). Populated at POST tools/call request time when
# PiiRedactor supplied entities; consumed synchronously by the GET-SSE stream
# handler so it can redact result frames BEFORE they reach the agent (without
# doing a blocking Redis GET on the event-loop thread). Each entry carries a
# monotonic-time inserted-at timestamp so `_pii_cache_put` can sweep expired
# entries — bounds memory growth on long-running proxies where flows may not
# reach `_write_mcp_result` (aborts, disconnects, malformed responses).
# mitmdump runs as a single process, so this cache is coherent with the
# pending Redis key.
_pii_entities_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def _pii_cache_put(session_id: str, jsonrpc_id: str, entities: list[str]) -> None:
    """Insert and opportunistically sweep entries older than MCP_PENDING_TTL."""
    now = time.monotonic()
    cutoff = now - MCP_PENDING_TTL
    # Sweep first (cheap — dict iteration; MCP_PENDING_TTL=120s bounds size).
    stale = [k for k, (ts, _) in _pii_entities_cache.items() if ts < cutoff]
    for k in stale:
        _pii_entities_cache.pop(k, None)
    _pii_entities_cache[(session_id, jsonrpc_id)] = (now, entities)


def _pii_cache_get(session_id: str, jsonrpc_id: str) -> list[str] | None:
    """Look up entities; returns None if absent or expired."""
    entry = _pii_entities_cache.get((session_id, jsonrpc_id))
    if entry is None:
        return None
    ts, entities = entry
    if time.monotonic() - ts > MCP_PENDING_TTL:
        _pii_entities_cache.pop((session_id, jsonrpc_id), None)
        return None
    return entities


def _pii_cache_pop(session_id: str, jsonrpc_id: str) -> None:
    """Best-effort removal; used by every terminal path."""
    _pii_entities_cache.pop((session_id, jsonrpc_id), None)


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


async def _write_mcp_result(mcp_session_id: str, jsonrpc_id: object, payload: str) -> tuple[dict | None, list[str]]:
    """Look up a pending MCP tool call by session+id and write the complete
    audit entry.

    Returns (redacted_data, entities) so the GET-SSE stream callback can also
    yield the redacted frame to the agent. entities is [] when the tool has
    no output rule; redacted_data is None when there is no matching pending
    key (protocol noise or TTL expired) or the payload isn't a JSON-RPC
    response — the caller should yield the original frame in those cases.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None, []

    # Only JSON-RPC responses carry both "id" and "result"/"error".
    if "id" not in data or ("result" not in data and "error" not in data):
        return None, []

    redis_key = f"mcp_pending:{mcp_session_id}:{data['id']}"
    try:
        r = await redis_client()
        pending_raw = await r.get(redis_key)
        if not pending_raw:
            return None, []  # no matching tool call (protocol noise, or TTL expired)

        await r.delete(redis_key)
        _pii_cache_pop(mcp_session_id, str(data["id"]))
        pending = json.loads(pending_raw)

        # If the POST tools/call had an output rule, PiiRedactor put the
        # entity list into the pending payload. Redact result/error before
        # writing the audit record AND before returning to the caller so the
        # agent sees the redacted frame.
        #
        # Fix #7: track whether redaction actually changed anything (vs. just
        # that a rule ran). `pii_redacted` on the record reflects actual PII
        # substitution, not the mere presence of a config.
        entities = pending.get("pii_output_entities") or []
        actually_redacted = False
        if entities:
            for key in ("result", "error"):
                if key in data:
                    before = data[key]
                    after = _safe_redact_json(before, entities)
                    if after != before:
                        actually_redacted = True
                    data[key] = after

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
            "pii_redacted":         "true" if actually_redacted else "false",
        }

        await r.xadd(f"audit:{pending['agent_id']}", record)
        await r.xadd("audit:global", record)
        return data, entities
    except (redis.RedisError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("AuditLog: MCP result write failed: %s", exc)
        return None, []


def _rewrite_get_sse_frame(
    frame_text: str,
    mcp_session_id: str,
) -> tuple[str, object | None, str]:
    """Redact a single GET-SSE frame in place and return the rewritten frame,
    plus the JSON-RPC id + original payload of the result line (for the audit
    task). Returns (rewritten_frame, None, "") for frames that carry no
    JSON-RPC result / no cached entities — nothing to redact and nothing to
    audit.
    """
    out_lines: list[str] = []
    jsonrpc_id: object | None = None
    audit_payload = ""
    for line in frame_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            out_lines.append(line)
            continue
        payload = stripped[5:].lstrip(" ")
        if not payload or payload == "[DONE]":
            out_lines.append(line)
            continue
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            out_lines.append(line)
            continue
        if not (isinstance(data, dict) and "id" in data and ("result" in data or "error" in data)):
            out_lines.append(line)
            continue
        # This is the frame we hand off to the audit writer.
        jsonrpc_id = data["id"]
        entities = _pii_cache_get(mcp_session_id, str(jsonrpc_id)) or []
        if entities:
            for key in ("result", "error"):
                if key in data:
                    data[key] = _safe_redact_json(data[key], entities)
            new_payload = json.dumps(data)
            out_lines.append(f"data: {new_payload}")
            audit_payload = new_payload
        else:
            out_lines.append(line)
            audit_payload = payload
    return "\n".join(out_lines), jsonrpc_id, audit_payload


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
        # PiiRedactor.request set this if the tool has an output rule. Persist
        # it into the pending payload so _write_mcp_result can redact result
        # frames, and mirror it into the in-process cache so the GET-SSE
        # stream handler can redact synchronously (no blocking Redis GET on
        # the event-loop thread).
        pii_output_entities = list(flow.metadata.get("amaze_pii_output_entities") or [])
        pending: dict[str, object] = {
            "trace_id":   trace_id,
            "span_id":    span_id,
            "agent_id":   agent_id,
            "session_id": session_id,
            "target":     target,
            "tool":       tool,
            "input":      raw_input,
            "ts":         str(int(time.time())),
        }
        if pii_output_entities:
            pending["pii_output_entities"] = pii_output_entities
            _pii_cache_put(mcp_session_id, str(jsonrpc_id), pii_output_entities)
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

        # PiiRedactor already set flow.response.stream to a redacting handler
        # that also writes the audit record. Do not overwrite it.
        if flow.metadata.get("amaze_pii_owned_stream"):
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
        """Streaming callback for GET SSE: correlates result frames with pending
        POST calls, writes audit records, and — when a POST tools/call had a
        PII output rule — redacts result frames BEFORE yielding to the agent.

        Redaction entity lookup is synchronous via `_pii_entities_cache`,
        populated at POST-request time by the AuditLog `request` hook (mirror
        of the same list stored inside the Redis pending payload). This avoids
        a blocking Redis GET on the event-loop thread.
        """
        mcp_session_id = flow.request.headers.get(MCP_SESSION_HEADER, "")
        buf = bytearray()
        sep: bytes | None = None  # pinned on first sighting (#6)

        def handle(chunk: bytes) -> bytes:
            nonlocal sep
            buf.extend(chunk)

            # Fix #6: pin separator on first sighting. Long-lived GET SSE
            # streams could otherwise glue frames together if the server
            # switched between LF and CRLF delimiters mid-stream.
            if sep is None:
                if b"\r\n\r\n" in buf:
                    sep = b"\r\n\r\n"
                elif b"\n\n" in buf:
                    sep = b"\n\n"
                else:
                    return b""

            emitted = bytearray()
            while sep in buf:
                idx = buf.index(sep)
                frame_bytes = bytes(buf[:idx])
                del buf[:idx + len(sep)]
                frame = frame_bytes.decode("utf-8", errors="replace")

                # Redact + rewrite the frame if any data: line carries a
                # JSON-RPC result and we have entities cached for that id.
                new_frame_text, jsonrpc_id_for_task, redact_payload = _rewrite_get_sse_frame(
                    frame, mcp_session_id,
                )
                emitted.extend(new_frame_text.encode("utf-8"))
                emitted.extend(sep)

                if jsonrpc_id_for_task is not None:
                    logger.info("AuditLog: GET SSE result frame id=%s session=%s",
                                jsonrpc_id_for_task,
                                mcp_session_id[:8] if mcp_session_id else "")
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(
                            _write_mcp_result(mcp_session_id, jsonrpc_id_for_task, redact_payload)
                        )
                    except RuntimeError as exc:
                        logger.warning("AuditLog: GET SSE task spawn failed: %s", exc)
            return bytes(emitted)

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
        # Set by PiiRedactor.request when input was mutated (this handler only
        # runs when PiiRedactor did NOT take ownership of the stream — i.e.,
        # tools with input-only rules).
        pii_redacted = "true" if flow.metadata.get("amaze_pii_redacted") else "false"
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
                            "pii_redacted":         pii_redacted,
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
            "pii_redacted":         "true" if flow.metadata.get("amaze_pii_redacted") else "false",
        }

        try:
            r = await redis_client()
            await r.xadd(f"audit:{agent_id}", record)
            await r.xadd("audit:global", record)
        except redis.RedisError as exc:
            logger.warning("AuditLog: redis write failed: %s", exc)


addons = [AuditLog()]
