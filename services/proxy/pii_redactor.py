"""
PiiRedactor addon — mutates MCP tool-call request params and response bodies
in flight using rules from `Policy.pii_config`.

Runs between StreamBlocker and Counters. Its `request` hook fires BEFORE
AuditLog so the pending Redis key AuditLog writes captures the redacted
input. Its `responseheaders` hook also fires before AuditLog's — for POST-SSE
tool responses PiiRedactor sets its own `flow.response.stream` callback and
signals AuditLog to leave the stream alone via
`flow.metadata["amaze_pii_owned_stream"] = True`. In that case PiiRedactor
writes the audit record itself, from inside the stream handler.

For buffered (non-SSE) responses PiiRedactor mutates
`flow.response.content` in place and lets AuditLog write the record with
the already-redacted content — no ownership transfer, just a
`flow.metadata["amaze_pii_redacted"] = True` marker for AuditLog to stamp
onto the record.

GET-SSE async results (tool call → 202 → later result on the long-lived GET
channel) are redacted inside AuditLog itself: PiiRedactor pins the applicable
output entity list on `flow.metadata["amaze_pii_output_entities"]`, AuditLog
persists it into the `mcp_pending:{session}:{id}` payload, and AuditLog's
GET-SSE handler reads it back to redact result frames before yielding and
before writing the audit record. This coupling lives on the AuditLog side
because AuditLog already owns the pending-key lifecycle and the GET-SSE
correlation.

Only fires on MCP `tools/call` flows (`amaze_kind == "mcp"` and
`amaze_mcp_tool` set). LLM and A2A traffic pass through untouched.

Failure handling: Presidio-level errors are logged at WARNING and the
affected field is replaced with `<PII_REDACTION_ERROR>` — never a DENY. The
enclosing FailClosed wrapper still catches real programming errors (attribute
errors, wrong body shape) and turns them into a 403.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import redis.asyncio as redis
from mitmproxy import http

from services.proxy import policy_store
from services.proxy._redis import client as redis_client
from services.proxy.pii_engine import (
    preload_ner_analyzer,
    redact,
    redact_json_text_fields,
    safe_redact_json,
)

logger = logging.getLogger(__name__)

MCP_SESSION_HEADER = "mcp-session-id"
_REDACT_ERROR_PLACEHOLDER = "<PII_REDACTION_ERROR>"


def _safe_redact(value: str, entities: list[str]) -> str:
    """Wrap redact() so a bad input becomes <PII_REDACTION_ERROR>, not a 403.
    Only used for STRING scalars where an error placeholder is a valid
    substitution. Do NOT use for dict/list — that's `safe_redact_json`."""
    try:
        return redact(value, entities)
    except Exception as exc:  # noqa: BLE001 — deliberately catch broad
        logger.warning("pii_redactor: redact() raised, dropping field: %s", exc)
        return _REDACT_ERROR_PLACEHOLDER


async def _write_audit_record(agent_id: str, record: dict) -> None:
    """Write an audit record. Mirrors audit_log._write_audit_record so PiiRedactor
    can emit records without importing private helpers from that module."""
    try:
        r = await redis_client()
        await r.xadd(f"audit:{agent_id}", record)
        await r.xadd("audit:global", record)
    except redis.RedisError as exc:
        logger.warning("pii_redactor: redis xadd failed: %s", exc)


class PiiRedactor:
    """See module docstring."""

    # ------------------------------------------------------------------- load

    def load(self, loader) -> None:  # noqa: ARG002 — mitmproxy hook signature
        """mitmproxy addon-load hook. Fires once at proxy startup, before any
        flow is dispatched. Eagerly warm the spaCy model here (#10) so the
        first user request doesn't block ~3-5 s on model init."""
        preload_ner_analyzer()

    # ------------------------------------------------------------------ request

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return
        if flow.metadata.get("amaze_bypass"):
            return
        if flow.metadata.get("amaze_kind") != "mcp":
            return
        tool_name = flow.metadata.get("amaze_mcp_tool", "")
        if not tool_name:
            return  # not a tools/call — nothing to redact

        agent_id = flow.metadata.get("amaze_agent")
        if not agent_id:
            return

        try:
            policy = await policy_store.get_policy(agent_id)
        except redis.RedisError as exc:
            logger.warning("pii_redactor: policy fetch failed, skipping: %s", exc)
            return
        if policy is None or policy.pii_config is None or not policy.pii_config.enabled:
            return

        rule = policy.pii_config.tools.get(tool_name)
        if rule is None:
            return

        # --- input redaction --------------------------------------------
        if rule.input:
            raw = flow.request.content or b""
            if raw:
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("pii_redactor: request body not JSON for tool=%s", tool_name)
                    body = None
                if body and body.get("method") == "tools/call":
                    args = body.get("params", {}).get("arguments") or {}
                    changed = False
                    for param, pii_rule in rule.input.items():
                        if not pii_rule.entities:
                            continue
                        if param not in args:
                            continue
                        value = args[param]
                        # Fix #12: walk nested container arguments too. Before,
                        # only top-level string params were touched, so a shape
                        # like {contact: {email: "..."}} would leak. Now dicts
                        # and lists are redacted leaf-by-leaf via the JSON walk.
                        if isinstance(value, str):
                            new_val = _safe_redact(value, list(pii_rule.entities))
                        elif isinstance(value, (dict, list)):
                            new_val = safe_redact_json(value, list(pii_rule.entities))
                        else:
                            continue
                        if new_val != value:
                            args[param] = new_val
                            changed = True
                    if changed:
                        body["params"]["arguments"] = args
                        # Fix #8: compact separators keep the wire size close
                        # to the original — the caller's exact byte-for-byte
                        # ordering is lost either way (json.dumps re-emits
                        # object keys in dict-insertion order), but we avoid
                        # inflating the payload with default `", "` gaps.
                        flow.request.content = json.dumps(
                            body, separators=(",", ":"), ensure_ascii=False,
                        ).encode()
                        flow.request.headers["content-length"] = str(len(flow.request.content))
                        flow.metadata["amaze_pii_redacted"] = True

        # --- output rule metadata (AuditLog GET-SSE handler + our own hooks read this)
        if rule.output and rule.output.entities:
            flow.metadata["amaze_pii_output_entities"] = list(rule.output.entities)

    # ------------------------------------------------------------ responseheaders

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        entities = list(flow.metadata.get("amaze_pii_output_entities") or [])
        if not entities:
            return
        if not flow.response:
            return

        ct = flow.response.headers.get("content-type", "").lower()
        is_sse = "text/event-stream" in ct

        # Only override the stream for POST inline SSE — that's the one AuditLog
        # would otherwise handle in _make_post_sse_record_writer. Buffered
        # bodies are mutated in response(). GET SSE (async result stream) is
        # redacted inside AuditLog via the pending-key payload.
        if is_sse and flow.request.method == "POST" and flow.response.status_code == 200:
            # Fix #5: keep `flow.response.stream` assignment and the ownership
            # flag in the SAME statement group. AuditLog reads the flag to
            # decide whether to install its own handler; if this pair ever
            # gets reordered so the flag is set without a handler being
            # installed, AuditLog will silently skip the audit record for
            # this flow. Assertion below is defense-in-depth against that.
            handler = self._make_post_sse_handler(flow, entities)
            flow.response.stream = handler
            flow.metadata["amaze_pii_owned_stream"] = True
            assert callable(flow.response.stream), \
                "amaze_pii_owned_stream set but no stream handler installed"

    def _make_post_sse_handler(self, flow: http.HTTPFlow, entities: list[str]):
        """Redact each `data:` frame's JSON-RPC result, re-serialize, yield,
        and write an audit record for the first result frame.

        Design notes:
          * **AuditLog stand-down via `amaze_pii_owned_stream`.** This handler
            replaces `AuditLog._make_post_sse_record_writer` for the flow;
            AuditLog's `responseheaders` checks the flag and skips setting
            its own stream handler. Historical note: earlier drafts named
            the flag `amaze_pii_owned_audit`; a straggling docstring or two
            may still reference that legacy name (see fix #16).
          * **Single audit record per span (intentional, #14).** MCP
            tools/call POST responses carry ONE JSON-RPC response per span.
            On the rare malformed server that emits several, only the first
            is audited — we log the subsequent ones at WARNING so the pattern
            shows up in monitoring rather than silently accumulating extras.
          * **Separator pinned on first sighting (#6).** SSE servers use
            either `\\n\\n` (LF) or `\\r\\n\\r\\n` (CRLF) as frame delimiters.
            A pathological server that switches mid-stream would previously
            get frames glued together; we now lock the separator to whichever
            appears first and stay with it.
          * **Cache cleanup on stream ownership (#4).** When PiiRedactor
            owns the POST-SSE stream, `_write_mcp_result` never runs, so the
            entities-cache entry AuditLog inserted at request-time would leak.
            We pop it once at handler construction.
        """
        # Capture identity + trace metadata at handler-construction time.
        agent_id   = flow.metadata.get("amaze_agent", "unknown")
        session_id = flow.metadata.get("amaze_session", "")
        tool       = flow.metadata.get("amaze_mcp_tool", "")
        target     = flow.metadata.get("amaze_mcp_server", "")

        span = flow.metadata.get("otel_span")
        if span is not None:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else ""
            span_id  = format(ctx.span_id,  "016x") if ctx.span_id  else ""
        else:
            trace_id = span_id = ""

        # --- Cache cleanup (#4). AuditLog.request inserted this entry so the
        #     GET-SSE code path could use it. For the POST-inline-SSE path we
        #     own audit and don't need the cache. Pop it here so the entry
        #     doesn't linger for 120 s until the natural TTL sweep.
        mcp_session_id = flow.request.headers.get(MCP_SESSION_HEADER, "")
        try:
            req_body = json.loads(flow.request.content or b"")
            jsonrpc_id = req_body.get("id")
        except (ValueError, TypeError):
            jsonrpc_id = None
        if mcp_session_id and jsonrpc_id is not None:
            # Break the import cycle by lazy-referencing audit_log's helper.
            from services.proxy.audit_log import _pii_cache_pop as _pop
            _pop(mcp_session_id, str(jsonrpc_id))

        # Input has already been redacted by request() at this point.
        raw_input = (flow.request.content or b"")[:8000].decode("utf-8", errors="replace")
        ts = str(int(time.time()))
        buf = bytearray()
        record_written = False
        sep: bytes | None = None  # pinned on first sighting (#6)

        def handle(chunk: bytes) -> bytes:
            nonlocal buf, record_written, sep
            buf.extend(chunk)

            # Fix #6: pin separator on first sighting. First delimiter type we
            # see wins for the rest of the stream.
            if sep is None:
                if b"\r\n\r\n" in buf:
                    sep = b"\r\n\r\n"
                elif b"\n\n" in buf:
                    sep = b"\n\n"
                else:
                    return b""  # no complete frame yet — keep buffering

            emitted = bytearray()
            while sep in buf:
                idx = buf.index(sep)
                frame_bytes = bytes(buf[:idx])
                del buf[:idx + len(sep)]
                frame_text = frame_bytes.decode("utf-8", errors="replace")

                redacted_frame_text, was_redacted = _redact_sse_frame(frame_text, entities)

                # Emit the (possibly-redacted) frame + pinned separator.
                emitted.extend(redacted_frame_text.encode("utf-8"))
                emitted.extend(sep)

                # Fix #7: every JSON-RPC result frame (even if it contained no
                # PII) needs exactly ONE audit record. Previously we only
                # wrote when was_redacted=True, so PII-free responses were
                # invisible to audit. Now every result frame produces one
                # record; `pii_redacted` reflects whether replacement actually
                # happened.
                #
                # Fix #14: single audit record per span is intentional — MCP
                # tools/call POST responses carry one JSON-RPC response. On a
                # malformed server that emits multiple, extras are logged and
                # skipped rather than accumulating audit records.
                parsed_frame = _extract_frame_json(redacted_frame_text)
                if parsed_frame is None:
                    continue  # keepalive / protocol noise / non-result data
                if record_written:
                    logger.warning(
                        "pii_redactor: additional JSON-RPC result frame in "
                        "POST SSE stream tool=%s trace=%s — skipping audit "
                        "(only the first result is audited)", tool, trace_id,
                    )
                    continue
                try:
                    result_obj = parsed_frame.get("result", parsed_frame.get("error", {}))
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
                        "pii_redacted":         "true" if was_redacted else "false",
                    }
                    loop = asyncio.get_running_loop()
                    loop.create_task(_write_audit_record(agent_id, record))
                    record_written = True
                except (ValueError, TypeError, RuntimeError) as exc:
                    logger.warning("pii_redactor: audit write from SSE frame failed: %s", exc)

            return bytes(emitted)

        return handle

    # ------------------------------------------------------------------ response

    async def response(self, flow: http.HTTPFlow) -> None:
        entities = list(flow.metadata.get("amaze_pii_output_entities") or [])
        if not entities:
            return
        if not flow.response:
            return
        if flow.metadata.get("amaze_pii_owned_stream"):
            return  # POST-SSE — the stream callback handled both redaction and audit
        ct = flow.response.headers.get("content-type", "").lower()
        if "text/event-stream" in ct:
            return  # GET-SSE — redacted inside AuditLog via pending-key payload

        raw = flow.response.content or b""
        if not raw:
            return

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            # Plain-text response — redact the whole payload.
            try:
                text = raw.decode("utf-8", errors="replace")
                redacted = _safe_redact(text, entities)
                new_bytes = redacted.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("pii_redactor: buffered plaintext redact failed: %s", exc)
                return
            flow.response.content = new_bytes
            flow.response.headers["content-length"] = str(len(new_bytes))
            flow.metadata["amaze_pii_redacted"] = True
            return

        redacted_body = safe_redact_json(body, entities)
        new_bytes = json.dumps(redacted_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        flow.response.content = new_bytes
        flow.response.headers["content-length"] = str(len(new_bytes))
        flow.metadata["amaze_pii_redacted"] = True


# ---------------------------------------------------------------------------
# SSE frame helpers
# ---------------------------------------------------------------------------

def _extract_frame_json(frame_text: str) -> dict | None:
    """Extract the JSON payload from an SSE frame's `data:` line. Returns None
    if the frame carries no parseable JSON-RPC response."""
    for line in frame_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].lstrip(" ")
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and "id" in data and ("result" in data or "error" in data):
            return data
    return None


def _redact_sse_frame(frame_text: str, entities: list[str]) -> tuple[str, bool]:
    """Rewrite `data: <json>` lines in one SSE frame with the JSON-RPC result
    (or error) walked for PII. Preserves other lines verbatim. Returns
    (rewritten_frame, was_any_data_line_redacted).
    """
    lines = frame_text.splitlines()
    changed_any = False
    out_lines: list[str] = []
    for line in lines:
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
        # Walk result / error for PII.
        for key in ("result", "error"):
            if key in data:
                data[key] = safe_redact_json(data[key], entities)
        new_payload = json.dumps(data)
        out_lines.append(f"data: {new_payload}")
        changed_any = True
    return "\n".join(out_lines), changed_any
