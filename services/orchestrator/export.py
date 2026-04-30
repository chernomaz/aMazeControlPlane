"""Export bundle assembly — Sprint S4 T6.1.

Builds a ZIP archive containing:

  * `traces.json` — list of full trace assemblies (one per distinct
    `trace_id` seen in the time range), shape identical to the trace-detail
    endpoint output (`assemble_trace`).
  * `audit.csv`   — one CSV row per audit record in the time range, columns
    matching the canonical audit_log.py field set.

Used by `services/orchestrator/routers/export.py` (route: `GET /export`)
which exposes this to the GUI's Export modal.

Caps:
  * At most `MAX_TRACES` (=1000) traces in `traces.json` to bound file size.
  * `input` and `output` columns truncated at `_CELL_TRUNCATE` (=4096) chars
    in the CSV to keep cells tractable in spreadsheets.

All Redis access flows through `audit_query` helpers (page-walks the
appropriate stream, decoding via the same projection as the read API).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from typing import Any

from services.orchestrator.audit_query import AuditRecord, list_audit_records
from services.orchestrator.trace_detail import assemble_trace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TRACES = 1000
_CELL_TRUNCATE = 4096
_PAGE_SIZE = 500

CSV_COLUMNS = (
    "stream_id",
    "trace_id",
    "span_id",
    "agent_id",
    "session_id",
    "kind",
    "target",
    "tool",
    "ts",
    "denied",
    "denial_reason",
    "indirect",
    "has_tool_calls_input",
    "alert",
    "input",
    "output",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_of(rec: AuditRecord) -> float:
    """Best-effort unix-seconds float from the record's `ts` string."""
    try:
        return float(rec.get("ts", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def _truncate(s: str, n: int = _CELL_TRUNCATE) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n]


async def _walk_records(
    agent_id: str | None,
    start: int,
    end: int,
) -> list[AuditRecord]:
    """Page through `list_audit_records` newest-first, stop once we cross
    below `start`. Returns records with `start <= ts <= end`, oldest-last
    (i.e. newest-first as walked)."""
    out: list[AuditRecord] = []
    cursor: str | None = None
    while True:
        page, next_cursor = await list_audit_records(
            agent_id=agent_id,
            limit=_PAGE_SIZE,
            cursor=cursor,
            denied_only=False,
        )
        if not page:
            break

        stop = False
        for rec in page:
            ts = _ts_of(rec)
            if ts and ts < start:
                # Past the lower bound — and since we walk newest-first,
                # any further entries are also out of range.
                stop = True
                break
            if ts and ts > end:
                # Above the upper bound — skip but keep walking, since
                # we may not be at the exact boundary yet.
                continue
            out.append(rec)

        if stop:
            break
        if next_cursor is None:
            break
        cursor = next_cursor
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_traces_json(records: list[AuditRecord]) -> bytes:
    """Assemble per-trace JSON list from a record set.

    Walks `records` in order, collects distinct `trace_id` values (capped
    at `MAX_TRACES`), then calls `assemble_trace` for each. Skips traces
    that resolve to None (no records — shouldn't happen since the trace_id
    came from the records, but defensive).
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for rec in records:
        tid = rec.get("trace_id", "")
        if not tid or tid in seen_set:
            continue
        seen_set.add(tid)
        seen.append(tid)
        if len(seen) >= MAX_TRACES:
            break

    traces: list[dict[str, Any]] = []
    for tid in seen:
        try:
            detail = await assemble_trace(tid)
        except Exception as exc:  # noqa: BLE001 — best-effort export path
            logger.warning("export: assemble_trace(%s) failed: %s", tid, exc)
            continue
        if detail is not None:
            traces.append(detail)

    return json.dumps(traces, ensure_ascii=False, default=str).encode("utf-8")


def build_audit_csv(records: list[AuditRecord]) -> bytes:
    """One CSV row per audit record. Columns per `CSV_COLUMNS`."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_COLUMNS)
    # Emit oldest-first for spreadsheet ergonomics (records came in newest-
    # first from the page-walk).
    for rec in reversed(records):
        row = [
            rec.get("_stream_id", ""),
            rec.get("trace_id", ""),
            rec.get("span_id", ""),
            rec.get("agent_id", ""),
            rec.get("session_id", ""),
            rec.get("kind", ""),
            rec.get("target", ""),
            rec.get("tool", ""),
            rec.get("ts", ""),
            rec.get("denied", ""),
            rec.get("denial_reason", ""),
            rec.get("indirect", ""),
            rec.get("has_tool_calls_input", ""),
            rec.get("alert", ""),
            _truncate(rec.get("input", "")),
            _truncate(rec.get("output", "")),
        ]
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


async def build_export_zip(
    agent_id: str | None,
    start: int,
    end: int,
    content: set[str],
) -> bytes:
    """Build the full ZIP archive bytes.

    `content` is the set of artifacts to include (subset of {"traces",
    "audit"}). At least one must be present; the router validates that.
    """
    records = await _walk_records(agent_id, start, end)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if "traces" in content:
            zf.writestr("traces.json", await build_traces_json(records))
        if "audit" in content:
            zf.writestr("audit.csv", build_audit_csv(records))
    return buffer.getvalue()
