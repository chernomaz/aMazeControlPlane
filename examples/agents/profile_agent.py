import os
from typing import Any

from dotenv import load_dotenv
load_dotenv()

from langchain.agents import create_agent as _lc_create_agent
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient

import amaze

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# Built once by on_startup hook after registration completes.
_agent = None


def _log(msg: str) -> None:
    print(f"[profile-agent] {msg}", flush=True)


async def _build_agent():
    global _agent
    _log("building agent (loading MCP tools)")
    client = MultiServerMCPClient(
        {
            "tools": {
                "url": "http://new-mcp/mcp/",   # registered MCP name — proxy resolves
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()
    _log(f"loaded {len(tools)} tools: {[t.name for t in tools]}")
    _agent = _lc_create_agent(
        model=llm,
        tools=tools,
        system_prompt=(
            "You are ProfileAgent. "
            "You must call the get_customer_profile tool to fetch the customer profile. "
            "Return only the useful customer summary."
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
    except Exception as e:
        _log(f"LLM call failed: {e}")
        return f"Error: {e}"

    _log(f"LLM returned: {content[:160]!r}")

    try:
        reply = amaze.send_message_to_agent("support-agent", content)
    except amaze.SendError as e:
        _log(f"forward to support-agent FAILED: status={e.status_code} reason={e.reason}")
        return f"forward to support-agent failed: status={e.status_code} reason={e.reason}"

    _log(f"support-agent replied: {str(reply)[:160]!r}")
    return reply


if __name__ == "__main__":
    amaze.init(on_startup=_build_agent)
