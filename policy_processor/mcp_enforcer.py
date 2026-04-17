"""
mcp_enforcer.py — MCP allow/deny decisions.

Sprint 3 rules:
  - Unknown caller                              → deny(unknown-caller)
  - MCP server not in caller's allowlist        → deny(mcp-server-not-allowed)
  - Tool not in caller's per-server tool list   → deny(tool-not-allowed)

Sprint 4 additions (applied after allow, in order):
  - body_size > limits.max_request_size_bytes   → deny(request-too-large)
  - requests > limits.max_requests_per_minute   → deny(rate-limit-exceeded)
    (window controlled by limits.rate_window_seconds, default 60)
  - tool calls > limits.per_tool_calls[tool]    → deny(call-limit-exceeded)
"""
from policy_processor.store import get_store
from policy_processor.limits import (
    check_request_size,
    get_call_counter,
    get_rate_limiter,
)


def decide(
    caller_id: str,
    server_id: str,
    tool_name: str,
    body_size: int = 0,
) -> tuple[bool, str]:
    policy = get_store().get_agent(caller_id)
    if policy is None:
        return False, "unknown-caller"

    allowed_servers = policy.get("allowed_mcp_servers") or []
    if server_id not in allowed_servers:
        return False, "mcp-server-not-allowed"

    allowed_tools = (policy.get("allowed_tools") or {}).get(server_id) or []
    if tool_name not in allowed_tools:
        return False, "tool-not-allowed"

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

    per_tool_limits = limits.get("per_tool_calls") or {}
    max_calls = per_tool_limits.get(tool_name)
    if max_calls is not None:
        ok, reason = get_call_counter().check_and_increment(
            caller_id, server_id, tool_name, int(max_calls)
        )
        if not ok:
            return False, reason

    return True, "ok"
