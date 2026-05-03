import os
import asyncio
from typing import Any

from dotenv import load_dotenv
load_dotenv()

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient

import amaze

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Built once by on_startup hook after registration completes.
# Handlers use _agent directly — no lazy build, no lock.
_agent = None


def _log(msg: str) -> None:
    print(f"[agent-sdk] {msg}", flush=True)


async def _build_agent():
    global _agent
    client = MultiServerMCPClient(
        {
            "tools": {
                "url": "http://demo-mcp:8000/mcp/",
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()
    _agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=(
            "You are a helpful research assistant. "
            "Use the web_search tool ONLY when you need up-to-date "
            "external facts. Do not call any other tool. "
            "Always cite sources."
        ),
    )
    _log(f"agent ready — {len(tools)} tool(s) loaded")


async def receive_message_from_user(q: Any) -> Any:
    _log(f"user message: {q!r}")
    if _agent is None:
        return "Agent not ready — please retry in a moment"
    try:
        result = await _agent.ainvoke(
            {"messages": [{"role": "user", "content": q}]}
        )
        content = result["messages"][-1].content
    except Exception as e:
        _log(f"LLM call failed: {e}")
        return f"Error: {e}"

    _log(f"LLM returned (len={len(str(content))}): {str(content)[:160]!r}")

    target = "agent-sdk1" if "bitcoin" in str(content).lower() else "agent-sdk2"
    _log(f"routing to {target}")

    try:
        reply = amaze.send_message_to_agent(target, content)
    except amaze.SendError as e:
        _log(f"forward to {target} FAILED: status={e.status_code} reason={e.reason}")
        return f"forward to {target} failed: status={e.status_code} reason={e.reason}"
    _log(f"{target} replied (len={len(str(reply))}): {str(reply)[:160]!r}")
    return reply


if __name__ == "__main__":
    amaze.init(on_startup=_build_agent)
