"""
Agent B — CrewAI-based A2A agent
Port : 9002
Role : Receives Agent A's hello, prints it, sends hello back to Agent A.

CrewAI usage
------------
- @tool  registers the A2A send capability in CrewAI's tool registry.
- Agent  declares identity, goal, backstory, and bound tools.
- In this demo the tool is invoked directly (no LLM required).
  In a full deployment set OPENAI_API_KEY and call crew.kickoff() so
  the LLM can reason over multi-step tasks using the registered tools.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from crewai import Agent
from crewai.tools import tool

from a2a.protocol import (
    build_request,
    build_response,
    build_error,
    extract_text,
    extract_reply_text,
)

AGENT_ID = "agent-b"
PORT = 9002
ENVOY_URL = "http://localhost:10000"   # all traffic routes through Envoy
AGENT_A_HOST = "agent-a"              # Host header selects the upstream cluster

app = FastAPI(title=f"A2A Agent — {AGENT_ID} (CrewAI)")


# ── CrewAI tool ────────────────────────────────────────────────────────────────

@tool("send_a2a_message")
def send_a2a_message(target_url: str, text: str, host: str = AGENT_A_HOST) -> str:
    """Send an A2A JSON-RPC message through Envoy and return the reply text."""
    payload = build_request(text)
    with httpx.Client(timeout=10) as client:
        resp = client.post(
            target_url,
            json=payload,
            headers={"x-agent-id": AGENT_ID, "Host": host},
        )
        resp.raise_for_status()
    return extract_reply_text(resp.json())


# ── CrewAI agent definition ────────────────────────────────────────────────────

agent_b = Agent(
    role="A2A Relay Agent",
    goal="Receive messages from other agents and reply via the A2A protocol.",
    backstory=(
        "I am a relay agent in the aMaze control plane. "
        "I receive A2A messages and respond in kind."
    ),
    tools=[send_a2a_message],
    verbose=False,
    llm="openai/gpt-4o",   # set OPENAI_API_KEY to enable LLM-driven reasoning
)


# ── A2A server endpoint ────────────────────────────────────────────────────────

@app.post("/")
async def receive(request: Request):
    body = await request.json()
    rpc_id = body.get("id", "1")
    params = body.get("params", {})
    task_id = params.get("id", "unknown")
    text = extract_text(body)
    print(f"{text}   received  from agent-a", flush=True)
    if not text:
        return JSONResponse(build_error(rpc_id, -32600, "empty message"), status_code=400)

    print(f"[{AGENT_ID}/CrewAI]   received hello from agent-a", flush=True)

    # Attempt to call back to agent-a (non-fatal if agent-a is not running).
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, lambda: send_a2a_message._run(target_url=ENVOY_URL, text="hello")
        )
    except Exception as exc:
        print(f"[{AGENT_ID}/CrewAI] callback to agent-a failed (non-fatal): {exc}", flush=True)

    return JSONResponse(build_response(task_id, rpc_id, f"hello back from {AGENT_ID}"))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
