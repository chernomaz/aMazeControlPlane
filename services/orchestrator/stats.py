"""Dashboard aggregation helpers — Sprint S4 T4.1.

Two async public coroutines that combine RedisTimeSeries metrics (written
by `services/proxy/counters.py`) with audit-stream walks (via
`services/orchestrator/audit_query.list_audit_records`) into the shapes
the GUI dashboard consumes.

Locked public surface (T4.2 / T4.3 routers import against this):

    async def get_dashboard_data(agent_id: str, range_seconds: int)
            -> DashboardPayload
    async def alerts_by_reason(agent_id: str | None, range_seconds: int)
            -> AlertsByReasonPayload

Source-of-truth keys consumed:

  * `ts:{agent_id}:llm_tokens`  — token count per LLM response
  * `ts:{agent_id}:tool_calls`  — 1 per MCP tool call
  * `ts:{agent_id}:a2a_calls`   — 1 per A2A call
  * `ts:{agent_id}:denials`     — 1 per denied request
  * `audit:{agent_id}` / `audit:global`  — full per-call records

Latency is not currently emitted to TS — see TODO at `_empty_latency_series`.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TypedDict

import redis.asyncio as redis

from services.orchestrator.audit_query import list_audit_records
from services.proxy._redis import client as redis_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed dicts (locked public shapes)
# ---------------------------------------------------------------------------


class KpiCard(TypedDict):
    calls: int                    # total LLM+MCP+A2A calls in range
    unique_tools: int             # distinct tool names invoked
    avg_latency_ms: int | None    # mean LLM latency in range; None until tracked
    critical_alerts: int          # count of denied=true in range
    policy_health: str            # "ok" | "warn" | "fail"


class TimePoint(TypedDict):
    ts: int                       # unix seconds, bucket start
    value: float                  # metric value in that bucket


class CategoryCount(TypedDict):
    label: str
    count: int


class TokenBucket(TypedDict):
    label: str                    # "<500", "500-1k", "1k-2k", ">2k"
    count: int
    color: str                    # palette hex


class DashboardPayload(TypedDict):
    kpi: KpiCard
    calls_over_time: list[TimePoint]
    latency_per_call: list[TimePoint]
    tokens: list[TokenBucket]
    tools: list[CategoryCount]
    a2a: list[CategoryCount]
    alerts: list[CategoryCount]


class AlertsByReasonPayload(TypedDict):
    total: int
    by_reason: list[CategoryCount]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUCKETS = 24

# Token-distribution palette — locked in the task spec.
_TOKEN_PALETTE: list[tuple[str, int, int, str]] = [
    # (label, lower_inclusive, upper_exclusive, color)
    ("<500",   0,    500,           "#2db5ff"),
    ("500-1k", 500,  1_000,         "#27d18d"),
    ("1k-2k",  1_000, 2_000,        "#f5b740"),
    (">2k",    2_000, 1 << 62,      "#ef4444"),
]

_TOP_N_BREAKDOWN = 8

# Audit walk cap. The dashboard summarises a bounded window (≤7d typical)
# but the underlying stream could be arbitrarily long; we hard-cap the
# number of records we visit so a single dashboard request can't sweep
# millions of entries.
_AUDIT_WALK_CAP = 5_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bucket_bounds(now_s: int, range_seconds: int) -> tuple[int, int, list[tuple[int, int]]]:
    """Return (from_s, bucket_size_s, list-of-(start_s, end_s)).

    24 evenly spaced buckets across `range_seconds`, ending at `now_s`.
    Bucket size = max(1, range_seconds // 24); a 1h range yields 24 × 150s
    buckets, 24h yields 24 × 3600s, 7d yields 24 × 25_200s.
    """
    bucket_size = max(1, range_seconds // _BUCKETS)
    # End-anchored: latest bucket ends at now_s.
    from_s = now_s - bucket_size * _BUCKETS
    bounds: list[tuple[int, int]] = []
    for i in range(_BUCKETS):
        start = from_s + i * bucket_size
        end = start + bucket_size
        bounds.append((start, end))
    return from_s, bucket_size, bounds


async def _ts_range(r: redis.Redis, key: str, from_ms: int, to_ms: int) -> list[tuple[int, float]]:
    """`TS.RANGE` wrapper — empty list if the key doesn't exist yet."""
    try:
        # redis-py's ts().range returns list[(ms, value)].
        return await r.ts().range(key, from_ms, to_ms)
    except redis.ResponseError as exc:
        # Key absent or wrong type — treat as no samples.
        logger.debug("ts.range %s [%d..%d] empty/failed: %s", key, from_ms, to_ms, exc)
        return []


def _bucketize_sum(samples: list[tuple[int, float]], bounds: list[tuple[int, int]]) -> list[float]:
    """Sum every sample value into its bucket. `samples` are (ms, val);
    bucket bounds are (start_s, end_s). One linear pass."""
    out = [0.0] * len(bounds)
    if not samples or not bounds:
        return out
    first_start = bounds[0][0]
    bucket_size = bounds[0][1] - bounds[0][0]
    for ts_ms, val in samples:
        ts_s = ts_ms // 1000
        if ts_s < first_start:
            continue
        idx = (ts_s - first_start) // bucket_size
        if 0 <= idx < len(out):
            out[idx] += float(val)
    return out


def _empty_latency_series(bounds: list[tuple[int, int]]) -> list[TimePoint]:
    """Best-effort latency series.

    TODO(S4): we don't currently emit per-call latency to RedisTimeSeries.
    Until a `ts:{agent_id}:llm_latency_ms` key exists, return zeros so the
    chart renders without faking values. KPI's `avg_latency_ms` is also
    None for the same reason.
    """
    return [TimePoint(ts=int(start), value=0.0) for start, _ in bounds]


def _token_bucket(total_tokens: int) -> int | None:
    """Return the index in `_TOKEN_PALETTE` for a given total_tokens, or
    None if non-positive (no usage info)."""
    if total_tokens <= 0:
        return None
    for idx, (_label, low, high, _color) in enumerate(_TOKEN_PALETTE):
        if low <= total_tokens < high:
            return idx
    return len(_TOKEN_PALETTE) - 1  # >= last upper bound — treat as ">2k"


def _parse_total_tokens(output_str: str) -> int:
    """Best-effort `usage.total_tokens` extraction. Mirror of the helper in
    audit_query, kept local so stats.py has no private cross-module deps."""
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


async def _walk_audit_in_range(
    agent_id: str | None, from_ts: float, range_seconds: int
) -> list[dict]:
    """Walk audit records newest-first, stop when entries fall before
    `from_ts`. Capped at `_AUDIT_WALK_CAP` records.

    Edge case: range_seconds may exceed the stream's lifespan (Redis
    Streams are unbounded by default but could be MAXLEN-trimmed in
    future). In that case we return whatever the stream still holds —
    no error.
    """
    out: list[dict] = []
    cursor: str | None = None
    page_size = 200

    while len(out) < _AUDIT_WALK_CAP:
        records, next_cursor = await list_audit_records(
            agent_id=agent_id, limit=page_size, cursor=cursor
        )
        if not records:
            break

        stop = False
        for rec in records:
            try:
                rec_ts = float(rec.get("ts", "") or 0)
            except (TypeError, ValueError):
                rec_ts = 0.0
            if rec_ts and rec_ts < from_ts:
                # Newest-first — once we cross from_ts everything older is
                # out of range. Stop walking.
                stop = True
                break
            out.append(rec)
            if len(out) >= _AUDIT_WALK_CAP:
                stop = True
                break

        if stop or next_cursor is None:
            break
        cursor = next_cursor

    return out


def _top_n_categories(counts: dict[str, int], n: int) -> list[CategoryCount]:
    """Sort by count desc (stable on tie via label asc) and trim to top n."""
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [CategoryCount(label=k, count=v) for k, v in items]


def _empty_token_distribution() -> list[TokenBucket]:
    return [
        TokenBucket(label=label, count=0, color=color)
        for label, _low, _high, color in _TOKEN_PALETTE
    ]


def _policy_health(critical_alerts: int) -> str:
    if critical_alerts == 0:
        return "ok"
    if critical_alerts <= 3:
        return "warn"
    return "fail"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_dashboard_data(agent_id: str, range_seconds: int) -> DashboardPayload:
    """Aggregate metrics + audit-stream extras into the dashboard payload.

    Use TS.RANGE for time-bucketed metrics (calls_over_time, latency_per_call,
    tokens). Use audit-stream walk (list_audit_records via audit_query) for
    per-tool / per-target / per-reason breakdowns. Keep range_seconds bounded
    (1h, 24h, 7d are typical).

    Empty data → return zeroed-but-shaped payload, never None.
    Redis errors → propagate (the route layer turns them into 503).
    """
    if range_seconds <= 0:
        range_seconds = 60  # defensive: still produce 24 buckets of 2-3s

    now_s = int(time.time())
    from_s, bucket_size, bounds = _bucket_bounds(now_s, range_seconds)
    from_ms = from_s * 1000
    to_ms = (from_s + bucket_size * _BUCKETS) * 1000

    r = await redis_client()

    # --- 1. Time-bucketed metrics (TS.RANGE) -------------------------------
    llm_samples = await _ts_range(r, f"ts:{agent_id}:llm_tokens", from_ms, to_ms)
    tool_samples = await _ts_range(r, f"ts:{agent_id}:tool_calls", from_ms, to_ms)
    a2a_samples = await _ts_range(r, f"ts:{agent_id}:a2a_calls", from_ms, to_ms)

    # `calls` per bucket = #LLM responses + #tool calls + #a2a calls.
    # The llm_tokens TS records one sample per LLM response with the
    # token count as value, so we count *samples* not values for the
    # call-count series.
    llm_count_per_bucket = _bucketize_sum(
        [(ts_ms, 1.0) for ts_ms, _ in llm_samples], bounds
    )
    tool_count_per_bucket = _bucketize_sum(
        [(ts_ms, 1.0) for ts_ms, _ in tool_samples], bounds
    )
    a2a_count_per_bucket = _bucketize_sum(
        [(ts_ms, 1.0) for ts_ms, _ in a2a_samples], bounds
    )

    calls_over_time: list[TimePoint] = []
    total_calls = 0
    for i, (start, _end) in enumerate(bounds):
        v = llm_count_per_bucket[i] + tool_count_per_bucket[i] + a2a_count_per_bucket[i]
        calls_over_time.append(TimePoint(ts=int(start), value=float(v)))
        total_calls += int(v)

    latency_per_call = _empty_latency_series(bounds)

    # --- 2. Audit-stream walk (per-record breakdowns) ----------------------
    records = await _walk_audit_in_range(agent_id, float(from_s), range_seconds)

    tools_counts: dict[str, int] = {}
    a2a_counts: dict[str, int] = {}
    alerts_counts: dict[str, int] = {}
    token_distribution_counts = [0] * len(_TOKEN_PALETTE)
    unique_tools: set[str] = set()
    critical_alerts = 0

    for rec in records:
        kind = rec.get("kind", "")
        denied = rec.get("denied") == "true"

        if denied:
            critical_alerts += 1
            reason = rec.get("denial_reason") or "unknown"
            alerts_counts[reason] = alerts_counts.get(reason, 0) + 1

        if kind == "mcp":
            tool = rec.get("tool") or ""
            if tool:
                unique_tools.add(tool)
                tools_counts[tool] = tools_counts.get(tool, 0) + 1
        elif kind == "a2a":
            target = rec.get("target") or ""
            if target:
                a2a_counts[target] = a2a_counts.get(target, 0) + 1
        elif kind == "llm":
            tokens = _parse_total_tokens(rec.get("output", ""))
            idx = _token_bucket(tokens)
            if idx is not None:
                token_distribution_counts[idx] += 1

    tokens_payload: list[TokenBucket] = [
        TokenBucket(
            label=label,
            count=token_distribution_counts[i],
            color=color,
        )
        for i, (label, _low, _high, color) in enumerate(_TOKEN_PALETTE)
    ]

    tools_payload = _top_n_categories(tools_counts, _TOP_N_BREAKDOWN)
    a2a_payload = _top_n_categories(a2a_counts, _TOP_N_BREAKDOWN)
    alerts_payload = _top_n_categories(alerts_counts, len(alerts_counts) or 1)
    # `_top_n_categories` with n>=len returns the full list; if there were
    # zero alerts we don't want a placeholder row, so clip empties.
    alerts_payload = [a for a in alerts_payload if a["count"] > 0]

    kpi: KpiCard = {
        "calls": total_calls,
        "unique_tools": len(unique_tools),
        # TODO(S4): switch to real mean once `ts:{agent_id}:llm_latency_ms`
        # is emitted by the tracer addon.
        "avg_latency_ms": None,
        "critical_alerts": critical_alerts,
        "policy_health": _policy_health(critical_alerts),
    }

    return DashboardPayload(
        kpi=kpi,
        calls_over_time=calls_over_time,
        latency_per_call=latency_per_call,
        tokens=tokens_payload or _empty_token_distribution(),
        tools=tools_payload,
        a2a=a2a_payload,
        alerts=alerts_payload,
    )


async def alerts_by_reason(
    agent_id: str | None, range_seconds: int
) -> AlertsByReasonPayload:
    """Group denied=true records in the range by `denial_reason`.

    If `agent_id` is None, scan the global stream; else `audit:{agent_id}`.
    Sorted desc by count.
    """
    if range_seconds <= 0:
        range_seconds = 60

    now_s = int(time.time())
    from_s = float(now_s - range_seconds)

    records = await _walk_audit_in_range(agent_id, from_s, range_seconds)

    counts: dict[str, int] = {}
    total = 0
    for rec in records:
        if rec.get("denied") != "true":
            continue
        reason = rec.get("denial_reason") or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
        total += 1

    by_reason = sorted(
        (CategoryCount(label=k, count=v) for k, v in counts.items()),
        key=lambda c: (-c["count"], c["label"]),
    )

    return AlertsByReasonPayload(total=total, by_reason=list(by_reason))
