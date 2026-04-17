"""
a2a_enforcer.py — A2A allow/deny decisions.

Sprint 2 rules:
  - Unknown caller                              → deny(unknown-caller)
  - Target not in caller's allowed_remote_agents → deny(not-allowed)

Sprint 4 additions (applied after allow, in order):
  - body_size > limits.max_request_size_bytes   → deny(request-too-large)
  - requests > limits.max_requests_per_minute   → deny(rate-limit-exceeded)
    (window controlled by limits.rate_window_seconds, default 60)
"""
from policy_processor.store import get_store
from policy_processor.limits import (
    check_request_size,
    get_rate_limiter,
)


def decide(caller_id: str, target_id: str, body_size: int = 0) -> tuple[bool, str]:
    policy = get_store().get_agent(caller_id)
    if policy is None:
        return False, "unknown-caller"

    allowed = policy.get("allowed_remote_agents") or []
    if target_id not in allowed:
        return False, "not-allowed"

    limits = policy.get("limits") or {}

    max_bytes = limits.get("max_request_size_bytes")
    if max_bytes is not None:
        ok, reason = check_request_size(body_size, int(max_bytes))
        if not ok:
            return False, reason

    max_rpm = limits.get("max_requests_per_minute")
    if max_rpm is not None:
        window = float(limits.get("rate_window_seconds", 60))
        ok, reason = get_rate_limiter().check(caller_id, int(max_rpm), window)
        if not ok:
            return False, reason

    return True, "ok"
