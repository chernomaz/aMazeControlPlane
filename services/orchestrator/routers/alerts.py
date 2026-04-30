"""GET /alerts — grouped-by-reason donut + denied-records table (S4-T4.3).

Backs the GUI's Alerts tab:

  * Donut chart   → `total` + `by_reason` (sorted desc) from
                    `services.orchestrator.stats.alerts_by_reason` (T4.1).
  * Records table → denied audit records within the same time window from
                    `services.orchestrator.audit_query.list_audit_records`
                    with `denied_only=True`, then range-filtered in-process.

Only `groupBy=reason` is implemented in this iteration; any other value
returns 400 (the GUI's range/group selectors are constrained to the
documented set, but we validate defensively for direct API consumers).

The `alert` field stored in the audit stream is a JSON string; we parse it
and surface the two most-used keys (`tool`, `target`) at the top level
alongside the full dict for convenience in the table renderer.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from services.orchestrator.audit_query import list_audit_records

logger = logging.getLogger(__name__)

router = APIRouter()


# Range tokens accepted by the alerts endpoint. Mirrors the per-agent stats
# endpoint (services/orchestrator/routers/agents.py:_RANGE_SECONDS) so the
# GUI can use a single range picker across tabs.
_RANGE_SECONDS: dict[str, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}

# Cap on records[]. The GUI's Alerts table paginates client-side, so we keep
# the server cap modest — 200 is plenty for one page and avoids fetching the
# whole stream when a noisy agent has thousands of denials.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

# When walking the audit stream for the records table we may have to skip
# entries that fall outside the requested window. Over-fetch by this factor
# so we still hit `limit` denied-and-in-window records in one call without
# pagination. 4x is empirically generous for the 1h window — the 24h/7d
# windows almost always include every denied entry the stream holds.
_OVERFETCH_MULTIPLIER = 4


def _parse_alert(raw: str) -> dict[str, Any]:
    """Parse the stored `alert` JSON; tolerate empty/malformed values."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _project_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Shape one denied AuditRecord into the response's records[] entry."""
    alert = _parse_alert(rec.get("alert", ""))
    try:
        ts = float(rec.get("ts", "") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    # `tool` and `target` live both at the top level of the audit record AND
    # inside the alert envelope (the proxy fills the latter on denial). Prefer
    # the audit-level value where present so we report the actual call target,
    # falling back to the alert dict for older records that omitted them.
    tool = rec.get("tool") or alert.get("tool")
    target = rec.get("target") or alert.get("target")
    return {
        "trace_id": rec.get("trace_id", ""),
        "agent_id": rec.get("agent_id", ""),
        "denial_reason": rec.get("denial_reason", ""),
        "ts": ts,
        "alert": alert,
        "tool": tool or None,
        "target": target or None,
    }


@router.get("/alerts")
async def get_alerts(
    agent: str | None = Query(default=None),
    range: str = Query(default="24h"),
    groupBy: str = Query(default="reason"),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    """Donut + denied records for the GUI's Alerts tab.

    Returns a single payload combining the grouped totals (for the donut)
    with a capped list of recent denied records (for the table beneath).
    Both views share the same range filter so click-through-by-reason
    queries stay consistent with the chart.
    """
    if groupBy != "reason":
        raise HTTPException(
            status_code=400,
            detail="invalid-groupBy; only 'reason' is supported",
        )

    range_seconds = _RANGE_SECONDS.get(range)
    if range_seconds is None:
        raise HTTPException(
            status_code=400,
            detail=f"invalid-range; expected one of {sorted(_RANGE_SECONDS)}",
        )

    # Lazy import — T4.1's stats module may not have shipped yet; degrade
    # to 503 rather than breaking router import at orchestrator startup.
    try:
        from services.orchestrator.stats import alerts_by_reason
    except ImportError as e:
        logger.error("get_alerts: stats module unavailable: %s", e)
        raise HTTPException(status_code=503, detail="stats-unavailable") from e

    try:
        grouped = await alerts_by_reason(agent, range_seconds)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — Redis hiccup → 503 fail-closed
        logger.error("get_alerts: alerts_by_reason failed: %s", e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    # alerts_by_reason() may return either a Pydantic model or a TypedDict —
    # mirror the agent_stats pattern (routers/agents.py:agent_stats).
    if hasattr(grouped, "model_dump"):
        grouped_dict = grouped.model_dump()
    elif hasattr(grouped, "dict"):
        grouped_dict = grouped.dict()
    else:
        grouped_dict = dict(grouped)

    total = int(grouped_dict.get("total", 0))
    by_reason = list(grouped_dict.get("by_reason", []))

    # Records table — pull denied-only, then filter to the requested window.
    cutoff = time.time() - range_seconds
    fetch_limit = min(limit * _OVERFETCH_MULTIPLIER, _MAX_LIMIT * _OVERFETCH_MULTIPLIER)
    try:
        denied_records, _ = await list_audit_records(
            agent_id=agent,
            limit=fetch_limit,
            denied_only=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("get_alerts: list_audit_records failed: %s", e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    records: list[dict[str, Any]] = []
    for rec in denied_records:
        try:
            ts = float(rec.get("ts", "") or 0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            # Stream is newest-first; once we cross the window we're done.
            break
        records.append(_project_record(rec))
        if len(records) >= limit:
            break

    return {
        "total": total,
        "by_reason": by_reason,
        "records": records,
    }
