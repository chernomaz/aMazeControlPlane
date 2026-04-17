"""
a2a_enforcer.py — A2A allow/deny decisions.

Sprint 2 rules:
  - Unknown caller (not in policy store) → deny(unknown-caller)
  - Target not in caller's allowed_remote_agents → deny(not-allowed)
  - Otherwise → allow
"""
from policy_processor.store import get_store


def decide(caller_id: str, target_id: str) -> tuple[bool, str]:
    policy = get_store().get_agent(caller_id)
    if policy is None:
        return False, "unknown-caller"
    allowed = policy.get("allowed_remote_agents") or []
    if target_id not in allowed:
        return False, "not-allowed"
    return True, "ok"
