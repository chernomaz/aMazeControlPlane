"""
mitmproxy addon loader.

Launched by supervisord as:

    mitmdump --listen-host 0.0.0.0 --listen-port 8080 \
             --set confdir=/opt/mitmproxy \
             -s services/proxy/main.py

Addon chain (in order):
  1. SessionIdentity — bearer → agent_id, strips spoofed x-amaze-caller.
  2. Tracer          — opens OTel span (request) BEFORE PolicyEnforcer so
                       even early denials carry a trace_id in the audit log.
                       Closes + exports the span on response.
  3. PolicyEnforcer  — allowlist checks + per-turn limit pre-checks.
                       Sets amaze_kind/amaze_target/amaze_mcp_tool that
                       downstream addons rely on.
  4. GraphEnforcer   — strict step ordering + atomic loop-limit reservation
                       (request hook); finalize / release reservation
                       (response hook based on 2xx vs non-2xx).
  5. StreamBlocker   — injects "stream": false into LLM request bodies.
  6. Counters        — RTS time-series metrics + per-turn integer counters.
  7. AuditLog        — XADD one record per call to Redis Streams (with
                       trace_id, alert, indirect, has_tool_calls_input).
  8. Router          — resolve logical target name → registered host:port
                       from Redis; rewrite flow.request.host + port before
                       mitmproxy opens the upstream connection. LLM flows
                       are a no-op (forwarded to real provider as-is).

FailClosed wraps every addon: if any `request` coroutine raises, the
wrapper turns the flow into a 403. Without this, mitmproxy passes through
on exception — the fail-open bug we are fixing.
"""
from __future__ import annotations

import logging
import sys
import traceback
from typing import Any

from mitmproxy import http

from services.proxy.audit_log import AuditLog
from services.proxy.counters import Counters
from services.proxy.deny import deny
from services.proxy.enforcer import PolicyEnforcer
from services.proxy.graph_enforcer import GraphEnforcer
from services.proxy.router import Router
from services.proxy.session import SessionIdentity
from services.proxy.stream_blocker import StreamBlocker
from services.proxy.tracer import Tracer

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("amaze.proxy")


class FailClosed:
    """Wraps a single addon so exceptions → 403 instead of pass-through."""

    def __init__(self, inner: Any, name: str) -> None:
        self._inner = inner
        self._name = name

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            # A previous wrapped addon already short-circuited. Skip.
            return
        method = getattr(self._inner, "request", None)
        if method is None:
            return
        try:
            await method(flow)
        except Exception:  # noqa: BLE001 — we explicitly fail closed
            logger.error(
                "addon %s raised on request — fail closed\n%s",
                self._name, traceback.format_exc(),
            )
            deny(flow, "internal-error", status=403)

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        method = getattr(self._inner, "responseheaders", None)
        if method is None:
            return
        try:
            await method(flow)
        except Exception:  # noqa: BLE001
            logger.error(
                "addon %s raised on responseheaders\n%s",
                self._name, traceback.format_exc(),
            )

    async def response(self, flow: http.HTTPFlow) -> None:
        method = getattr(self._inner, "response", None)
        if method is None:
            return
        try:
            await method(flow)
        except Exception:  # noqa: BLE001
            # Response-side failures do NOT deny — the upstream already
            # answered. But they are logged so bugs surface.
            logger.error(
                "addon %s raised on response\n%s",
                self._name, traceback.format_exc(),
            )


addons = [
    # Order matters. SessionIdentity resolves the bearer first so every
    # downstream addon has agent_id + session_id available.
    FailClosed(SessionIdentity(), "session"),
    # Tracer runs BEFORE PolicyEnforcer so that even when the enforcer
    # denies (short-circuiting the chain), the span has already been opened
    # and the audit record can be tagged with the conversation's trace_id.
    # Without this, denied records had empty trace_ids and were invisible
    # in the traces UI when users tried to debug "why did this fail?".
    FailClosed(Tracer(), "tracer"),
    FailClosed(PolicyEnforcer(), "enforcer"),
    FailClosed(GraphEnforcer(), "graph"),
    FailClosed(StreamBlocker(), "stream_blocker"),
    FailClosed(Counters(), "counters"),
    FailClosed(AuditLog(), "audit_log"),
    # Router always runs last — after all enforcement and audit. If any
    # earlier addon denied the request, the FailClosed guard above skips
    # Router. The audit record therefore always records the logical name
    # (e.g. "agent-sdk1"), never the resolved IP.
    FailClosed(Router(), "router"),
]
