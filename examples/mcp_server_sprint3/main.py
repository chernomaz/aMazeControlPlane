"""
Sprint 3 MCP server — fastmcp / streamable-http on :9003.

Tools:
  echo           — allowed (policy permits agent-a-mcp to call it)
  dangerous_tool — blocked by policy before this server ever sees the request
"""
import sys
from fastmcp import FastMCP

mcp = FastMCP("sprint3-mcp")


@mcp.tool()
def echo(text: str) -> str:
    """Return the input text unchanged."""
    print(f"[mcp-server] echo called: text={text!r}", flush=True)
    return text


@mcp.tool()
def dangerous_tool() -> str:
    """This tool must never execute — the policy processor should block it."""
    print("[mcp-server] DANGEROUS_TOOL EXECUTED — policy enforcement FAILED", file=sys.stderr, flush=True)
    return "dangerous result"


if __name__ == "__main__":
    print("[mcp-server] starting on 127.0.0.1:9003 (streamable-http /mcp)", flush=True)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=9003)
