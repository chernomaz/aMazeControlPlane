"""
mcp_enforcer.py — MCP allow/deny decisions.

Sprint 3 rules:
  - Unknown caller (not in policy store)       → deny(unknown-caller)
  - MCP server not in caller's allowlist        → deny(mcp-server-not-allowed)
  - Tool not in caller's per-server tool list   → deny(tool-not-allowed)
  - Otherwise                                   → allow
"""
from policy_processor.store import get_store


def decide(caller_id: str, server_id: str, tool_name: str) -> tuple[bool, str]:
    policy = get_store().get_agent(caller_id)
    if policy is None:
        return False, "unknown-caller"

    allowed_servers = policy.get("allowed_mcp_servers") or []
    if server_id not in allowed_servers:
        return False, "mcp-server-not-allowed"

    allowed_tools = (policy.get("allowed_tools") or {}).get(server_id) or []
    if tool_name not in allowed_tools:
        return False, "tool-not-allowed"

    return True, "ok"
