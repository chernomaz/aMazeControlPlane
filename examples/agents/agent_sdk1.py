import os
import asyncio
from typing import Any

from dotenv import load_dotenv
load_dotenv()

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient

import amaze


def _log(msg: str) -> None:
    print(f"[agent-sdk1] {msg}", flush=True)


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Built once by on_startup hook after registration completes.
_agent = None


async def _build_agent():
    global _agent
    _log("building LangChain agent (loading MCP tools)")
    client = MultiServerMCPClient(
        {
            "tools": {
                "url": "http://demo-mcp:8000/mcp/",
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()
    _log(f"loaded {len(tools)} tools: {[t.name for t in tools]}")
    _agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=(
            "You are a helpful research assistant. "
            "Use the web_search tool ONLY when you need up-to-date "
            "external facts. Do not call any other tool — the proxy "
            "will deny non-allowed calls. "
            "Always cite sources."
        ),
    )
    _log("agent ready")


async def receive_message_from_user(q: Any) -> Any:
    _log(f"user message: {q!r}")
    if _agent is None:
        return "Agent not ready — please retry in a moment"
    try:
        result = await _agent.ainvoke(
            {"messages": [{"role": "user", "content": q}]}
        )
        content = str(result["messages"][-1].content)
        _log(f"LLM returned (len={len(content)}): {content[:200]!r}")
        return content
    except Exception as e:
        _log(f"LLM call failed: {e}")
        return f"Error: {e}"


async def receive_message_from_agent(caller: str, q: Any) -> Any:
    _log(f"A2A from {caller}: {q!r}")
    return await receive_message_from_user(q)


if __name__ == "__main__":
    amaze.init(on_startup=_build_agent)
