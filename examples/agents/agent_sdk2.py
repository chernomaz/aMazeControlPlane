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
    print(f"[agent-sdk2] {msg}", flush=True)


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Lazy-build: policy isn't pushed at module-import time, so the MCP
# handshake would fail. First inbound call (from agent-sdk via A2A)
# triggers the build; subsequent calls reuse the cached agent.
_agent = None


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
            "You are a task-dispatcher assistant. "
            "When asked for weather, call web_search with the query you are given. "
            "When asked to read mail, call dummy_email with the person name. "
            "Return a concise, factual answer with the result."
        ),
    )
    return _agent


async def _run_llm(task: str) -> str:
    agent = await _build_agent()
    try:
        with tracing_context(enabled=False):
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": task}]}
            )
        content = str(result["messages"][-1].content)
        _log(f"LLM returned (len={len(content)}): {content[:200]!r}")
        return content
    except Exception as e:
        _log(f"LLM call failed: {e}")
        return f"Error: {e}"


async def receive_message_from_user(q: Any) -> Any:
    # agent-sdk2 is designed as a cascade target, not a user-facing endpoint.
    # We still honour direct /chat requests by running the same dispatcher
    # logic — useful for isolation testing (`curl http://localhost:<port>/chat`).
    _log(f"user message: {q!r}")
    return await _dispatch(q)


async def receive_message_from_agent(caller: str, q: Any) -> Any:
    _log(f"A2A message from {caller}: {q!r}")
    return await _dispatch(q)


async def _dispatch(q: Any) -> Any:
    """Decide which tool to trigger based on the incoming text, then run the LLM.

    - `weather` anywhere in the message → ask the LLM to fetch current
      weather for London via web_search.
    - anything else → ask the LLM to read alice's mailbox via dummy_email
      and return the content.
    """
    lower = q.lower()
    if "weather" in lower:
        task = (
            "Use web_search to find the current weather in London, UK. "
            "Summarise the result in one or two sentences."
        )
        _log("branch = weather (web_search London)")
    else:
        task = (
            "Use dummy_email with person='alice' to read Alice's emails, "
            "then return the full email content verbatim."
        )
        _log("branch = mail (dummy_email alice)")
    return await _run_llm(task)


if __name__ == "__main__":
    amaze.init(on_startup=_build_agent)
