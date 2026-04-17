"""
Agent A — LangChain-based A2A agent
Port : 9001
Role : Initiates the hello exchange; receives Agent B's reply.

LangChain usage
---------------
- StructuredTool wraps the A2A HTTP call as a typed, reusable tool.
- RunnableLambda composes the tool into a one-step chain that can be
  extended with prompt templates, memory, or other chain components.
"""
import asyncio
import sys
import os
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableLambda

from a2a.protocol import (
    build_request,
    build_response,
    build_error,
    extract_text,
    extract_reply_text,
)

AGENT_ID = "agent-a"
PORT = 9001
ENVOY_URL = "http://localhost:10000"   # all traffic routes through Envoy
AGENT_B_HOST = "agent-b"              # Host header selects the upstream cluster


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_run_hello())
    yield


app = FastAPI(title=f"A2A Agent — {AGENT_ID} (LangChain)", lifespan=lifespan)


# ── LangChain tool: send A2A message ─────────────────────────────────────────

class SendMessageInput(BaseModel):
    target_url: str
    text: str


def _send_a2a(target_url: str, text: str, host: str = AGENT_B_HOST) -> str:
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


send_a2a_tool = StructuredTool.from_function(
    func=_send_a2a,
    name="send_a2a_message",
    description="Send an A2A message to another agent and return its reply.",
    args_schema=SendMessageInput,
)

# One-step LangChain chain — invoke the tool and return the reply.
# In a full agent this chain would be preceded by a prompt template and
# an LLM reasoning step; here the action is deterministic for demo clarity.
hello_chain = RunnableLambda(
    lambda _: send_a2a_tool.invoke({"target_url": ENVOY_URL, "text": "hello"})
)


# ── A2A server endpoint ───────────────────────────────────────────────────────

@app.post("/")
async def receive(request: Request):
    body = await request.json()
    rpc_id = body.get("id", "1")
    params = body.get("params", {})
    task_id = params.get("id", "unknown")
    text = extract_text(body)

    if not text:
        return JSONResponse(build_error(rpc_id, -32600, "empty message"), status_code=400)

    print(f"[{AGENT_ID}/LangChain] received hello from agent-b", flush=True)

    return JSONResponse(build_response(task_id, rpc_id, "acknowledged"))


# ── Hello exchange (triggered from lifespan) ─────────────────────────────────

async def _run_hello():
    await asyncio.sleep(6.0)   # give Agent B time to start (CrewAI init ~4s)
    print(f"[{AGENT_ID}/LangChain] sending 'hello' → agent-b …", flush=True)
    loop = asyncio.get_event_loop()
    reply = await loop.run_in_executor(None, hello_chain.invoke, None)
    # reply is whatever Agent B put in artifacts[0].parts[0].text
    # The print for "received hello from b" happens in the /  endpoint above
    # (Agent B calls us back separately); this reply is the HTTP ack.
    _ = reply   # acknowledged


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
