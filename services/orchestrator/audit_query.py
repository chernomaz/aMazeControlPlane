"""Audit log query helpers — Sprint S4 T1.3.

Async helpers wrapping XRANGE / XREVRANGE against the audit Redis Streams
written by `services/proxy/audit_log.py`. Stream keys:

  * `audit:{agent_id}` — per-agent stream
  * `audit:global`     — every record, mirrored

Each stream entry's fields match the dict at audit_log.py:143-159
(trace_id, span_id, agent_id, session_id, kind, target, tool, input, output,
ts, denied, denial_reason, alert, indirect, has_tool_calls_input). All values
are stored as strings.

Three public coroutines:

  * `list_audit_records()` — newest-first, cursor-paginated, optional
                             agent + denied-only filter.
  * `list_traces()`        — newest-first trace-summary projection
                             (one row per trace_id).
  * `get_trace_records()`  — every entry for a single trace_id, oldest-first.

The orchestrator route layer (T1.2) imports against this module's exact
signatures.
"""
from __future__ import annotations

import json
import logging
from typing import TypedDict

import redis.asyncio as redis

from services.proxy._redis import client as redis_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed dicts
# ---------------------------------------------------------------------------


class AuditRecord(TypedDict, total=False):
    trace_id: str
    span_id: str
    agent_id: str
    session_id: str
    kind: str            # llm | mcp | a2a
    target: str
    tool: str
    input: str
    output: str
    ts: str              # unix epoch as string
    denied: str          # "true" | "false"
    denial_reason: str
    alert: str           # JSON-encoded
    indirect: str        # "true" | "false"
    has_tool_calls_input: str
    _stream_id: str


class TraceSummary(TypedDict):
    trace_id: str
    agent_id: str
    started_at: float
    duration_ms: int
    llm_calls: int
    tool_calls: int
    a2a_calls: int
    total_tokens: int
    violations: int
    status: str          # "passed" | "failed"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Required by the AuditRecord projection. We tolerate missing optional fields
# (`alert`, `indirect`, `has_tool_calls_input`) since older entries written
# by earlier audit_log.py revisions may not have them — but the four below
# are load-bearing for every downstream consumer (UI list + trace grouping).
_REQUIRED_FIELDS = ("trace_id", "agent_id", "kind", "ts")


def _stream_key(agent_id: str | None) -> str:
    return f"audit:{agent_id}" if agent_id else "audit:global"


def _decode(value) -> str:
    """Best-effort string coercion. The shared `redis_client()` is created
    with `decode_responses=True`, so values are normally str already. Guard
    bytes too in case a future caller swaps the pool."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _entry_to_record(stream_id, fields: dict) -> AuditRecord | None:
    """Convert one (id, dict) Redis stream tuple into our AuditRecord shape.
    Returns None if a required field is missing (malformed entry) — caller
    skips it but keeps walking."""
    rec: AuditRecord = {}
    for k, v in fields.items():
        rec[_decode(k)] = _decode(v)  # type: ignore[literal-required]

    for required in _REQUIRED_FIELDS:
        # trace_id may legitimately be empty if the tracer addon failed to
        # open a span (rare, but keep the entry visible in the records list);
        # require everything else.
        if required == "trace_id":
            if "trace_id" not in rec:
                return None
            continue
        if not rec.get(required):  # type: ignore[literal-required]
            return None

    rec["_stream_id"] = _decode(stream_id)
    return rec


def _parse_total_tokens(output_str: str) -> int:
    """Best-effort `usage.total_tokens` extraction from an LLM JSON body.
    Any parse error → 0. Never raises."""
    if not output_str:
        return 0
    try:
        body = json.loads(output_str)
    except (ValueError, TypeError):
        return 0
    if not isinstance(body, dict):
        return 0
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens", 0)
    try:
        return int(total)
    except (TypeError, ValueError):
        return 0


def _decrement_id(stream_id: str) -> str:
    """Return the largest stream id strictly smaller than `stream_id`.

    Redis stream ids are `ms-seq`. To page backwards through XREVRANGE
    without re-reading the boundary entry, we pass the previous batch's
    smallest id, decremented, as the new `max=`. Decrement seq when
    possible, else step ms back and use a large seq sentinel."""
    try:
        ms_str, seq_str = stream_id.split("-", 1)
        ms = int(ms_str)
        seq = int(seq_str)
    except (ValueError, AttributeError):
        # Pathological id — fall back to using it as-is. Caller may see one
        # duplicate at most, then exhaust normally.
        return stream_id
    if seq > 0:
        return f"{ms}-{seq - 1}"
    if ms > 0:
        # Step back one ms; use max seq sentinel so we cover every entry
        # in the prior millisecond.
        return f"{ms - 1}-18446744073709551615"
    return "0-0"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_audit_records(
    agent_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    denied_only: bool = False,
) -> tuple[list[AuditRecord], str | None]:
    """Newest-first page of audit records.

    `cursor` is a Redis Stream entry id; pass `None` (or `"-"`) to start from
    the newest entry. The returned `next_cursor` is an id strictly smaller
    than the oldest record returned in this page (suitable as the next
    call's `cursor`), or `None` when the stream is exhausted.

    With `denied_only=True` we filter client-side after pulling each chunk;
    we may need to over-fetch to fill `limit`, so we keep walking the
    stream in batches until we have enough denied rows or run out.
    """
    if limit <= 0:
        return [], None

    key = _stream_key(agent_id)
    r = await redis_client()

    if cursor is None or cursor == "-":
        max_id: str = "+"
    else:
        max_id = cursor

    chunk = limit * 4 if denied_only else limit

    out: list[AuditRecord] = []
    last_seen: str | None = None
    exhausted = False

    while len(out) < limit:
        try:
            entries = await r.xrevrange(key, max=max_id, min="-", count=chunk)
        except redis.ResponseError as exc:
            # Stream doesn't exist or other key-shape error → empty result.
            logger.debug("list_audit_records: xrevrange %s failed: %s", key, exc)
            return out, None

        if not entries:
            exhausted = True
            break

        for stream_id, fields in entries:
            rec = _entry_to_record(stream_id, fields)
            if rec is None:
                last_seen = _decode(stream_id)
                continue
            last_seen = rec["_stream_id"]
            if denied_only and rec.get("denied") != "true":
                continue
            out.append(rec)
            if len(out) >= limit:
                break

        if len(entries) < chunk:
            exhausted = True
            break

        if last_seen is None:
            exhausted = True
            break
        max_id = _decrement_id(last_seen)

    if exhausted:
        return out, None
    next_cursor = _decrement_id(last_seen) if last_seen is not None else None
    return out, next_cursor


async def list_traces(
    agent_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    denied_only: bool = False,
) -> tuple[list[TraceSummary], str | None]:
    """Group records by `trace_id`, return one summary per trace, newest-first.

    Always scans `audit:global` so that peer-agent denials (e.g. agent-sdk2
    denying a tool call during an A2A hop initiated by agent-sdk) are included
    in the violation count and the trace status. Per-agent streams only hold
    records from that agent, so they miss peer denials on the same trace_id.

    When `agent_id` is provided only traces that have at least one record
    belonging to that agent are returned, but ALL records for those traces
    (including peer records) are used for the summary metrics.

    `denied_only=True` includes only traces that contain at least one denied
    record. Filtering happens after grouping so a trace with one denial is
    still returned in full.
    """
    if limit <= 0:
        return [], None

    r = await redis_client()

    if cursor is None or cursor == "-":
        max_id: str = "+"
    else:
        max_id = cursor

    # trace_id -> all records (any agent), newest-first encounter order.
    grouped: dict[str, list[AuditRecord]] = {}
    # trace_ids confirmed to have ≥1 record from the requested agent.
    owned: set[str] = set()
    order: list[str] = []  # owned trace_ids in encounter order
    last_seen: str | None = None
    chunk = max(limit * 8, 100)
    exhausted = False

    while len(order) < limit:
        try:
            entries = await r.xrevrange("audit:global", max=max_id, min="-", count=chunk)
        except redis.ResponseError as exc:
            logger.debug("list_traces: xrevrange audit:global failed: %s", exc)
            exhausted = True
            break

        if not entries:
            exhausted = True
            break

        for stream_id, fields in entries:
            rec = _entry_to_record(stream_id, fields)
            if rec is None:
                last_seen = _decode(stream_id)
                continue
            last_seen = rec["_stream_id"]
            tid = rec.get("trace_id", "")
            if not tid:
                continue

            if tid not in grouped:
                grouped[tid] = []
            grouped[tid].append(rec)

            rec_agent = rec.get("agent_id", "")
            if tid not in owned and (agent_id is None or rec_agent == agent_id):
                owned.add(tid)
                if len(order) < limit:
                    order.append(tid)

        if len(entries) < chunk:
            exhausted = True
            break

        if last_seen is None:
            exhausted = True
            break
        max_id = _decrement_id(last_seen)

    summaries: list[TraceSummary] = []
    for tid in order:
        summary = _summarize_trace(tid, grouped[tid], primary_agent_id=agent_id)
        if denied_only and summary["violations"] == 0:
            continue
        summaries.append(summary)

    if exhausted:
        next_cursor: str | None = None
    else:
        next_cursor = _decrement_id(last_seen) if last_seen is not None else None
    return summaries, next_cursor


def _summarize_trace(
    trace_id: str,
    records: list[AuditRecord],
    primary_agent_id: str | None = None,
) -> TraceSummary:
    """Project a list of AuditRecords (one trace) into a TraceSummary."""
    # Use the provided primary agent_id when available (caller knows which
    # agent owns this trace). Fall back to first non-empty agent_id seen.
    agent_id = primary_agent_id or ""
    timestamps: list[float] = []
    llm_calls = tool_calls = a2a_calls = 0
    total_tokens = 0
    violations = 0

    for rec in records:
        if not agent_id and rec.get("agent_id"):
            agent_id = rec["agent_id"]
        try:
            ts = float(rec.get("ts", "") or 0)
            if ts > 0:
                timestamps.append(ts)
        except (TypeError, ValueError):
            pass

        kind = rec.get("kind", "")
        if kind == "llm":
            llm_calls += 1
            total_tokens += _parse_total_tokens(rec.get("output", ""))
        elif kind == "mcp":
            tool_calls += 1
        elif kind == "a2a":
            a2a_calls += 1

        if rec.get("denied") == "true":
            violations += 1

    if timestamps:
        started = min(timestamps)
        duration_ms = int(round((max(timestamps) - started) * 1000))
    else:
        started = 0.0
        duration_ms = 0

    return TraceSummary(
        trace_id=trace_id,
        agent_id=agent_id,
        started_at=started,
        duration_ms=duration_ms,
        llm_calls=llm_calls,
        tool_calls=tool_calls,
        a2a_calls=a2a_calls,
        total_tokens=total_tokens,
        violations=violations,
        status="failed" if violations > 0 else "passed",
    )


async def get_trace_records(
    trace_id: str,
    agent_id: str | None = None,
) -> list[AuditRecord]:
    """All audit-stream entries for a single `trace_id`, oldest-first.

    Walks the full stream forward (`XRANGE - +`) and filters by trace_id.
    Used by the Trace detail view in T3.4. No pagination — a single trace
    is small (a handful of hops).
    """
    if not trace_id:
        return []

    key = _stream_key(agent_id)
    r = await redis_client()

    out: list[AuditRecord] = []
    cursor_min: str = "-"
    chunk = 500

    while True:
        try:
            entries = await r.xrange(key, min=cursor_min, max="+", count=chunk)
        except redis.ResponseError as exc:
            logger.debug("get_trace_records: xrange %s failed: %s", key, exc)
            return out

        if not entries:
            break

        for stream_id, fields in entries:
            rec = _entry_to_record(stream_id, fields)
            if rec is None:
                continue
            if rec.get("trace_id") == trace_id:
                out.append(rec)

        if len(entries) < chunk:
            break

        # Continue strictly after the largest id we just saw.
        last_id = _decode(entries[-1][0])
        cursor_min = f"({last_id}"  # Redis exclusive-min syntax

    return out
