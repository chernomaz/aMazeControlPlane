"""
limits.py — in-memory rate limiter, size checker, and call counter.

Sprint 4 enforcement (applied after policy allow):
  check order: size → rate → call count

All state is process-local and resets on Policy Processor restart.
All classes are thread-safe (the gRPC server uses a thread pool).
"""
import threading
import time
from collections import defaultdict, deque


# ── Size ──────────────────────────────────────────────────────────────────────

def check_request_size(body_size: int, max_bytes: int) -> tuple[bool, str]:
    if body_size > max_bytes:
        return False, "request-too-large"
    return True, "ok"


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Sliding-window rate limiter keyed by agent_id.

    window_seconds defaults to 60 but can be overridden per-check so tests
    can use a short window (e.g. 3 s) without special-casing the code.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, deque] = defaultdict(deque)

    def check(
        self,
        agent_id: str,
        max_requests: int,
        window_seconds: float = 60.0,
    ) -> tuple[bool, str]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            w = self._windows[agent_id]
            while w and w[0] < cutoff:
                w.popleft()
            if len(w) >= max_requests:
                return False, "rate-limit-exceeded"
            w.append(now)
        return True, "ok"


# ── Call counter ──────────────────────────────────────────────────────────────

class CallCounter:
    """
    Monotonically increasing per-(agent, server, tool) call counter.

    Counts only successful (allowed) calls; resets on process restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple, int] = defaultdict(int)

    def check_and_increment(
        self,
        agent_id: str,
        server_id: str,
        tool_name: str,
        max_calls: int,
    ) -> tuple[bool, str]:
        key = (agent_id, server_id, tool_name)
        with self._lock:
            if self._counts[key] >= max_calls:
                return False, "call-limit-exceeded"
            self._counts[key] += 1
        return True, "ok"


# ── Singletons ────────────────────────────────────────────────────────────────
# Initialized at module level — import lock guarantees thread-safe construction.

_rate_limiter = RateLimiter()
_call_counter = CallCounter()


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter


def get_call_counter() -> CallCounter:
    return _call_counter
