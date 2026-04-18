"""
demo_agent — integration demo for aMaze Control Plane

LangChain-based agent that exercises Envoy policy enforcement end-to-end:

  Step 1 — LLM call       : ChatOpenAI generates a research question
  Step 2 — MCP web_search : calls web_search tool on search-mcp (port 9004)
                             through Envoy, enforced by Policy Processor
  Step 3 — MCP echo       : calls echo tool on mcp-server (port 9003)
                             through Envoy, enforced by Policy Processor
  Step 4 — A2A to agent-b : sends tasks/send to agent-b (port 9002)
                             through Envoy, enforced by Policy Processor

All outbound traffic goes through Envoy :10000.
The agent identity is carried via x-agent-id: demo-agent.
Host header selects the upstream cluster in Envoy.

Prerequisites (run run_demo_integration.sh first):
  - Envoy          :10000
  - Policy Processor :50051
  - MCP server (sprint3, echo)      :9003  →  Host: mcp-server
  - MCP server (main, web_search)   :9004  →  Host: search-mcp
  - agent-b                         :9002  →  Host: agent-b
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root (walk up from examples/agents/demo_agent/)
load_dotenv(Path(__file__).parents[3] / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from a2a.protocol import build_request, extract_reply_text

ENVOY_URL = "http://localhost:10000"
AGENT_ID = "demo-agent"
MCP_PATH = "/mcp"


# ── A2A tool (LangChain StructuredTool wrapping httpx) ───────────────────────

class A2AInput(BaseModel):
    text: str
    target_host: str = "agent-b"


def _send_a2a(text: str, target_host: str = "agent-b") -> str:
    """Send a tasks/send message through Envoy and return the reply text."""
    payload = build_request(text)
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            ENVOY_URL,
            json=payload,
            headers={"x-agent-id": AGENT_ID, "Host": target_host},
        )
        resp.raise_for_status()
    return extract_reply_text(resp.json())


send_a2a_tool = StructuredTool.from_function(
    func=_send_a2a,
    name="send_a2a_message",
    description=(
        "Send a message to another agent via A2A JSON-RPC through Envoy "
        "and return its reply. Use target_host='agent-b' to reach agent-b."
    ),
    args_schema=A2AInput,
)


# ── Demo ─────────────────────────────────────────────────────────────────────

async def run_demo() -> None:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    # Connect to both MCP servers through Envoy.
    # The 'host' header routes each connection to the right Envoy cluster.
    # langchain-mcp-adapters 0.1.0+ removed context-manager support;
    # get_tools() opens a fresh MCP session per tool call automatically.
    mcp = MultiServerMCPClient(
        {
            "search-mcp": {
                "transport": "streamable_http",
                "url": f"{ENVOY_URL}{MCP_PATH}",
                "headers": {
                    "x-agent-id": AGENT_ID,
                    "host": "search-mcp",
                },
            },
            "mcp-server": {
                "transport": "streamable_http",
                "url": f"{ENVOY_URL}{MCP_PATH}",
                "headers": {
                    "x-agent-id": AGENT_ID,
                    "host": "mcp-server",
                },
            },
        }
    )
    mcp_tools = await mcp.get_tools()
    tool_names = [t.name for t in mcp_tools]
    print(f"[demo-agent] MCP tools loaded from Envoy: {tool_names}")

    # ── Step 1: LLM call ─────────────────────────────────────────────────────
    print("\n" + "─" * 55)
    print("[Step 1] LLM call — generating research question")
    print("─" * 55)

    llm_resp = await llm.ainvoke([
        SystemMessage(content="You are a concise research assistant."),
        HumanMessage(
            content=(
                "Generate a single focused research question about AI agent "
                "safety in one sentence. Return only the question."
            )
        ),
    ])
    query = llm_resp.content.strip()
    print(f"  → {query}")

    # ── Step 2: MCP web_search through Envoy → search-mcp ────────────────────
    print("\n" + "─" * 55)
    print("[Step 2] web_search via search-mcp through Envoy")
    print("─" * 55)

    web_tool = next(t for t in mcp_tools if t.name == "web_search")
    search_result = await web_tool.ainvoke({"query": query})
    search_text = str(search_result)
    print(f"  → {search_text[:300]}{'...' if len(search_text) > 300 else ''}")

    # ── Step 3: MCP echo through Envoy → mcp-server (sprint3) ────────────────
    print("\n" + "─" * 55)
    print("[Step 3] echo via mcp-server through Envoy")
    print("─" * 55)

    echo_tool = next(t for t in mcp_tools if t.name == "echo")
    echo_result = await echo_tool.ainvoke({"text": f"Query confirmed: {query}"})
    print(f"  → {echo_result}")

    # ── Step 4: A2A message → agent-b through Envoy ───────────────────────────
    print("\n" + "─" * 55)
    print("[Step 4] A2A tasks/send → agent-b through Envoy")
    print("─" * 55)

    a2a_reply = await asyncio.get_running_loop().run_in_executor(
        None,
        _send_a2a,
        f"Research complete. Topic: {query[:120]}",
    )
    print(f"  → agent-b replied: {a2a_reply}")

    print("\n" + "=" * 55)
    print("Demo complete — all 4 steps executed through Envoy.")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(run_demo())
