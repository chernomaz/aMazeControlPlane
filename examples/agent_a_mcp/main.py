"""
agent_a_mcp — Sprint 3 demo agent.

Calls MCP tools through Envoy (proxy at :10000) with x-agent-id: agent-a-mcp
and Host: mcp-server so Envoy routes to the MCP server cluster.

Expected outcome:
  echo          → allowed  → prints result
  dangerous_tool → blocked  → prints 403 denial
"""
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ENVOY_MCP_URL = "http://localhost:10000/mcp"
AGENT_ID = "agent-a-mcp"
MCP_SERVER_HOST = "mcp-server"


async def main() -> None:
    transport = StreamableHttpTransport(
        url=ENVOY_MCP_URL,
        headers={
            "x-agent-id": AGENT_ID,
            "host": MCP_SERVER_HOST,
        },
    )

    async with Client(transport) as client:
        print(f"[{AGENT_ID}] calling echo tool...", flush=True)
        try:
            result = await client.call_tool("echo", {"text": "hello from agent-a-mcp"})
            print(f"[{AGENT_ID}] echo result: {result}", flush=True)
        except Exception as exc:
            print(f"[{AGENT_ID}] echo failed (unexpected): {exc}", flush=True)

        print(f"[{AGENT_ID}] calling dangerous_tool (should be blocked)...", flush=True)
        try:
            result = await client.call_tool("dangerous_tool", {})
            print(f"[{AGENT_ID}] dangerous_tool result (UNEXPECTED): {result}", flush=True)
        except Exception as exc:
            print(f"[{AGENT_ID}] dangerous_tool denied (expected): {exc}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
