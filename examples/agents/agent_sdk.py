import os

# Disable LangSmith tracing BEFORE any langchain import — inside a NEMO
# container the tracer tries to POST to api.smith.langchain.com over HTTPS,
# which tunnels through Envoy as CONNECT and hangs the agent.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)

import asyncio
from typing import Any

from dotenv import load_dotenv
load_dotenv()

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langsmith import tracing_context

import amaze

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# The agent is built lazily on the first inbound message: at module-import
# time the orchestrator has not yet pushed policy, so the MCP handshake
# would be denied. By the first `receive_message_from_user` call, policy
# is live and the MCP `get_tools` call succeeds.
_agent = None
_agent_lock = asyncio.Lock()


async def _build_agent():
    global _agent
    async with _agent_lock:
        if _agent is None:
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
    return _agent


def _log(msg: str) -> None:
    # print + flush is enough — Docker captures stdout. Using a plain
    # print keeps the log format tight and avoids fighting uvicorn's
    # logging configuration.
    print(f"[agent-sdk] {msg}", flush=True)


async def receive_message_from_user(q: Any) -> Any:
    _log(f"user message: {q!r}")
    agent = await _build_agent()
    try:
        # tracing_context(enabled=False) defensively disables tracing even
        # inside the block — the env vars at the top of the file should be
        # enough, but the belt-and-suspenders costs one word.
        with tracing_context(enabled=False):
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": q}]}
            )
        content = result["messages"][-1].content
    except Exception as e:
        _log(f"LLM call failed: {e}")
        return f"Error: {e}"

    _log(f"LLM returned (len={len(str(content))}): {str(content)[:160]!r}")

    # Content-based routing:
    #   'bitcoin' anywhere in the LLM output → forward to agent-sdk1
    #   anything else                         → forward to agent-sdk2
    #
    # Case-insensitive match; the LLM might phrase the topic as
    # "Bitcoin", "BITCOIN", "bitcoin price", etc.
    if "bitcoin" in str(content).lower():
        target = "agent-sdk1"
    else:
        target = "agent-sdk2"
    _log(f"routing to {target}")

    # amaze.send_message_to_agent is sync but safe to call from an async
    # handler — internally it forks a one-shot thread when there's a
    # running event loop. For a tight hot path, use asyncio.to_thread to
    # offload to the default pool; here a per-call thread is fine.
    try:
        reply = amaze.send_message_to_agent(target, content)
    except amaze.SendError as e:
        _log(f"forward to {target} FAILED: status={e.status_code} reason={e.reason}")
        return f"forward to {target} failed: status={e.status_code} reason={e.reason}"
    _log(f"{target} replied (len={len(str(reply))}): {str(reply)[:160]!r}")
    return reply


if __name__ == "__main__":
    amaze.init()
