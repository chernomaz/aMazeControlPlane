"""GET /export — Sprint S4 T6.1.

Streams a ZIP archive containing `traces.json` (Jaeger-format-ish, mirroring
the trace-detail endpoint output) and/or `audit.csv` (one row per audit
record). The GUI's Export modal calls this directly.

Query contract:
  agent     str, optional   filter by agent_id; default = all agents
  start     int, optional   unix epoch seconds; default = end - 86400
  end       int, optional   unix epoch seconds; default = now
  content   str, optional   csv subset of {traces, audit}; default both
  format    str, optional   only "zip" supported

Errors:
  400 invalid-content      — unknown token in `content`
  400 invalid-format       — anything other than "zip"
  400 invalid-range        — start > end, or negative bound
  400 empty-content        — `content` resolved to empty set
  503 redis-unavailable    — Redis I/O blew up while walking the stream
"""
from __future__ import annotations

import logging
import re
import time

import redis.asyncio as redis
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from services.orchestrator.export import build_export_zip

# Sanitise label fragments that flow into the Content-Disposition filename.
# Anything outside a conservative ASCII set is replaced with `_` so a crafted
# `agent` query param can't break out of the header value.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_CONTENT = {"traces", "audit"}
_VALID_FORMAT = {"zip"}
_DEFAULT_WINDOW_SEC = 24 * 60 * 60


@router.get("/export")
async def export(
    agent: str | None = Query(default=None),
    start: int | None = Query(default=None),
    end: int | None = Query(default=None),
    content: str = Query(default="traces,audit"),
    format: str = Query(default="zip"),
) -> Response:
    if format not in _VALID_FORMAT:
        raise HTTPException(
            status_code=400,
            detail="invalid-format; only zip supported",
        )

    raw_tokens = [t.strip() for t in content.split(",") if t.strip()]
    if not raw_tokens:
        raise HTTPException(status_code=400, detail="empty-content")
    unknown = [t for t in raw_tokens if t not in _VALID_CONTENT]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail="invalid-content; allowed: traces, audit",
        )
    content_set = set(raw_tokens)

    now = int(time.time())
    end_ts = end if end is not None else now
    start_ts = start if start is not None else (end_ts - _DEFAULT_WINDOW_SEC)

    if start_ts < 0 or end_ts < 0:
        raise HTTPException(status_code=400, detail="invalid-range")
    if start_ts > end_ts:
        raise HTTPException(status_code=400, detail="invalid-range")

    try:
        body = await build_export_zip(
            agent_id=agent,
            start=start_ts,
            end=end_ts,
            content=content_set,
        )
    except redis.RedisError as exc:
        logger.error("export: redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="redis-unavailable") from exc

    label = _FILENAME_SAFE.sub("_", agent) if agent else "all"
    filename = f"amaze-export-{label}-{start_ts}-{end_ts}.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    return Response(content=body, media_type="application/zip", headers=headers)
