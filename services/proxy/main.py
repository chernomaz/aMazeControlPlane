"""
mitmproxy addon loader.

Launched by supervisord as:

    mitmdump --listen-host 0.0.0.0 --listen-port 8080 \
             --set confdir=/opt/mitmproxy \
             -s services/proxy/main.py

mitmdump imports this module and calls `addons = [...]`. We register:
  1. SessionIdentity — bearer → agent_id, strips spoofed x-amaze-caller.
  2. PolicyEnforcer  — the single decision point; deny or inject caller.
  3. Counters        — request+token metrics; does not gate traffic.

FailClosed wraps the three addons: if any `request` / `response` coroutine
raises unexpectedly, the wrapper turns the flow into a 403 response.
Without this, mitmproxy would log the exception and pass the request
through — which is exactly the aMaze-audit fail-open bug we are fixing.
"""
from __future__ import annotations

import logging
import sys
import traceback
from typing import Any

from mitmproxy import http

from services.proxy.counters import Counters
from services.proxy.deny import deny
from services.proxy.enforcer import PolicyEnforcer
from services.proxy.session import SessionIdentity

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
    FailClosed(SessionIdentity(), "session"),
    FailClosed(PolicyEnforcer(), "enforcer"),
    FailClosed(Counters(), "counters"),
]
