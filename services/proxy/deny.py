"""Helpers for building consistent 403 deny responses on a mitmproxy flow.

Every deny uses the same JSON envelope:

    { "error": "denied", "reason": "<code>", ...extra }

The `reason` codes match the failure-handling table in CLAUDE.md §8 and the
acceptance criteria in SPRINTS.md. Keep these stable — tests key off them.
"""
from __future__ import annotations

import json
from typing import Any

from mitmproxy import http


def deny(
    flow: http.HTTPFlow,
    reason: str,
    status: int = 403,
    **extra: Any,
) -> None:
    """Short-circuit the flow with a structured deny response."""
    body: dict[str, Any] = {"error": "denied", "reason": reason, **extra}
    flow.response = http.Response.make(
        status,
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
