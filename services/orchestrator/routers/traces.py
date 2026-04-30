"""GET /traces — paginated audit projection (one row per trace_id).
GET /traces/{trace_id} — full assembly for the GUI's Trace detail page.

The list endpoint delegates to `services.orchestrator.audit_query.list_traces`.
The detail endpoint delegates to
`services.orchestrator.trace_detail.assemble_trace`, which walks the audit
stream for one trace_id and projects it into the shape consumed by the
Trace detail page (T3.5) — see TRACE_DATA in services/ui_mock/index.html.

Cursor model: Redis Stream IDs. Caller passes the previous response's
`next_cursor` back as `?offset=`. We accept `offset` as the cursor name
because the GUI's URL contract was specified that way; internally it's a
stream ID, not a numeric offset.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from services.orchestrator.audit_query import list_traces
from services.orchestrator.trace_detail import assemble_trace

router = APIRouter()


@router.get("/traces")
async def get_traces(
    agent: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: str | None = Query(default=None),
    denied_only: bool = Query(default=False),
) -> dict[str, Any]:
    traces, next_cursor = await list_traces(
        agent_id=agent,
        limit=limit,
        cursor=offset,
        denied_only=denied_only,
    )
    return {"traces": traces, "next_cursor": next_cursor}


@router.get("/traces/{trace_id}")
async def get_trace_detail(trace_id: str) -> dict[str, Any]:
    """Full trace projection for the GUI's Trace detail page.

    Walks the audit stream once via `assemble_trace` and returns a record
    matching the mock's TRACE_DATA shape: 3-col summary, sequence_steps,
    edges, violations_list, plus the live policy snapshot for the primary
    agent. 404 when no audit records exist for `trace_id`.
    """
    detail = await assemble_trace(trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="trace-not-found")
    return detail
