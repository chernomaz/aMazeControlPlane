import os

# Disable LangSmith tracing BEFORE any langchain import — inside a NEMO
# container the tracer tries to POST to api.smith.langchain.com over HTTPS,
# which tunnels through Envoy as CONNECT and hangs the agent.
import asyncio
from typing import Any

from dotenv import load_dotenv
load_dotenv()

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langsmith import tracing_context

import amaze


def _log(msg: str) -> None:
    print(f"[agent-sdk1] {msg}", flush=True)


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Same lazy-build pattern as agent_sdk.py — the MCP handshake can't
# happen at module-import time because policy hasn't been pushed yet.
_agent = None
_agent_lock = asyncio.Lock()


async def _build_agent():
    global _agent
    if _agent is not None:
        return _agent
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
    return _agent


async def receive_message_from_user(q: Any) -> Any:
    _log(f"user message: {q!r}")
    agent = await _build_agent()
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": q}]}
        )
        content = str(result["messages"][-1].content)
        _log(f"LLM returned (len={len(content)}): {content[:200]!r}")
        return content
    except Exception as e:
        _log(f"LLM call failed: {e}")
        return f"Error: {e}"


# Inbound A2A from another agent is handled exactly like a user message:
# run the LLM on it and return the content as the A2A reply. This makes
# agent-sdk1 the terminal hop — it doesn't forward anywhere further.
#
# A direct alias `receive_message_from_agent = receive_message_from_user`
# wouldn't work because the SDK calls this handler with TWO args
# (caller_id, message) while receive_message_from_user takes one. Wrap
# so we accept the caller id and just drop it.
async def receive_message_from_agent(caller: str, q: Any) -> Any:
    _log(f"A2A from {caller}: {q!r}")
    return await receive_message_from_user(q)


if __name__ == "__main__":
    amaze.init(on_startup=_build_agent)
