"""NEMO container agent — Phase 8A.

Minimal agent runtime that exercises the full NEMO lifecycle:

  1. On startup, POSTs to the Orchestrator /agents/register with its identity
     and container-internal ports.
  2. Until the Orchestrator reports status RUNNING, the agent's chat and A2A
     endpoints return HTTP 503 (agent-not-ready).
  3. Once RUNNING, chat and A2A endpoints handle traffic.

All outbound HTTP goes through Envoy via HTTP_PROXY (set in the container env),
so A2A calls between agents are enforced by the Policy Processor in real time.

This agent is intentionally dumb: chat returns a canned reply, A2A echoes
the incoming message. Sprint 9 replaces it with the proper SDK-driven agent
implementation; Phase 8A only needs to prove the lifecycle and enforcement
plumbing works end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn


AGENT_ID = os.environ["AMAZE_AGENT_ID"]
CONTAINER_HOST = os.environ.get("AMAZE_CONTAINER_HOST", AGENT_ID)
CHAT_PORT = int(os.environ.get("AMAZE_CHAT_PORT", "8080"))
A2A_PORT = int(os.environ.get("AMAZE_A2A_PORT", "9002"))
ORCHESTRATOR_URL = os.environ["AMAZE_ORCHESTRATOR_URL"].rstrip("/")

# Envoy proxy is set via HTTP_PROXY/HTTPS_PROXY env — httpx picks this up
# automatically. We read AMAZE_PROXY only for logging.
AMAZE_PROXY = os.environ.get("AMAZE_PROXY", "")


class AgentState:
    """Simple lifecycle flag flipped by the registration loop."""

    def __init__(self) -> None:
        self.status = "PENDING"
        self.ready_event = threading.Event()

    def mark_running(self) -> None:
        self.status = "RUNNING"
        self.ready_event.set()


state = AgentState()


def register_and_poll() -> None:
    """Background worker: register with orchestrator, then poll until RUNNING.

    This is NOT done via HTTP_PROXY — the orchestrator is a control-plane
    endpoint reachable directly on the NEMO network.
    """
    body = json.dumps(
        {
            "agent_id": AGENT_ID,
            "host": CONTAINER_HOST,
            "chat_port": CHAT_PORT,
            "a2a_port": A2A_PORT,
        }
    ).encode()

    # Retry registration in case the orchestrator isn't up yet.
    for attempt in range(30):
        try:
            req = urllib.request.Request(
                f"{ORCHESTRATOR_URL}/agents/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read())
                print(f"[nemo-agent {AGENT_ID}] registered: {payload}", flush=True)
                if payload.get("status") == "RUNNING":
                    state.mark_running()
                    return
                break
        except (urllib.error.URLError, ConnectionError) as e:
            print(f"[nemo-agent {AGENT_ID}] register attempt {attempt+1} failed: {e}", flush=True)
            time.sleep(2)
    else:
        print(f"[nemo-agent {AGENT_ID}] giving up on registration", flush=True)
        return

    # Poll status until RUNNING.
    while True:
        try:
            with urllib.request.urlopen(
                f"{ORCHESTRATOR_URL}/agents/{AGENT_ID}/status", timeout=3
            ) as resp:
                payload = json.loads(resp.read())
                if payload.get("status") == "RUNNING":
                    print(f"[nemo-agent {AGENT_ID}] policy received -> RUNNING", flush=True)
                    state.mark_running()
                    return
        except Exception as e:
            print(f"[nemo-agent {AGENT_ID}] status poll error: {e}", flush=True)
        time.sleep(1)


# ── Chat app (port 8080) ─────────────────────────────────────────────────────

chat_app = FastAPI()


@chat_app.post("/chat")
async def chat(req: Request) -> JSONResponse:
    if state.status != "RUNNING":
        return JSONResponse(
            {"error": "agent-not-ready", "reason": "awaiting-policy"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    body = await req.json()
    msg = body.get("message", "")
    reply = f"[{AGENT_ID}] received user message: {msg!r}"
    return JSONResponse({"reply": reply})


@chat_app.get("/healthz")
async def chat_health() -> dict[str, str]:
    return {"status": state.status, "agent_id": AGENT_ID}


# ── A2A app (port 9002) ──────────────────────────────────────────────────────
#
# JSON-RPC 2.0 over HTTP, method "tasks/send". Shape matches
# examples/agents/a2a/protocol.py so the existing A2A helpers work.

a2a_app = FastAPI()


@a2a_app.post("/")
async def a2a(req: Request) -> JSONResponse:
    if state.status != "RUNNING":
        return JSONResponse(
            {"error": "agent-not-ready", "reason": "awaiting-policy"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    body = await req.json()
    rpc_id = body.get("id", "0")
    method = body.get("method", "")
    if method != "tasks/send":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "method not supported"}},
        )

    params = body.get("params") or {}
    message = params.get("message") or {}
    parts = message.get("parts") or []
    text = parts[0].get("text", "") if parts else ""

    reply = f"[{AGENT_ID}] echo: {text}"
    resp = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "id": params.get("id", rpc_id),
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": reply}]}],
        },
    }
    return JSONResponse(resp)


# Client helper: send A2A message to another agent through Envoy.
# Exposed on the chat app as /a2a-to/{target} for demo purposes (Phase 8A).

@chat_app.post("/a2a-to/{target}")
async def a2a_to(target: str, req: Request) -> JSONResponse:
    if state.status != "RUNNING":
        return JSONResponse(
            {"error": "agent-not-ready"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    body = await req.json()
    text = body.get("message", "")
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tasks/send",
        "params": {
            "id": "task-1",
            "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
        },
    }
    # Envoy is the HTTP proxy (HTTP_PROXY env var); we hit the target agent by
    # its A2A virtual-host known to Envoy: http://{target}:9002. Envoy runs
    # ext_proc before routing, so Policy Processor sees caller/target even for
    # non-routed targets and can deny with 403 before the upstream is tried.
    envoy = os.environ.get("AMAZE_PROXY", "http://proxy:10000")
    target_url = f"http://{target}:{A2A_PORT}/"
    headers = {
        "Content-Type": "application/json",
        "x-agent-id": AGENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=15, proxy=envoy) as client:
            resp = await client.post(target_url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        # Transport-level failure (DNS, proxy-connect, timeout). Distinct from
        # an Envoy policy denial, which still returns a status_code.
        return JSONResponse(
            {"status_code": None, "error": f"{type(e).__name__}: {e}"},
            status_code=200,
        )
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    return JSONResponse(
        {"status_code": resp.status_code, "body": body},
        status_code=200,
    )


# ── Launcher ─────────────────────────────────────────────────────────────────

def run_servers() -> None:
    print(f"[nemo-agent {AGENT_ID}] starting chat:{CHAT_PORT} a2a:{A2A_PORT} proxy={AMAZE_PROXY}", flush=True)

    async def _serve() -> None:
        chat_config = uvicorn.Config(chat_app, host="0.0.0.0", port=CHAT_PORT, log_level="warning")
        a2a_config = uvicorn.Config(a2a_app, host="0.0.0.0", port=A2A_PORT, log_level="warning")
        chat_server = uvicorn.Server(chat_config)
        a2a_server = uvicorn.Server(a2a_config)

        chat_task = asyncio.create_task(chat_server.serve())
        a2a_task = asyncio.create_task(a2a_server.serve())

        # Don't start the register/poll loop until both sockets are accepting.
        # Otherwise state can flip to RUNNING while the A2A port is still
        # binding, and incoming A2A hits Envoy → upstream connect refused.
        while not (chat_server.started and a2a_server.started):
            await asyncio.sleep(0.05)
        threading.Thread(target=register_and_poll, daemon=True).start()

        await asyncio.gather(chat_task, a2a_task)

    asyncio.run(_serve())


if __name__ == "__main__":
    run_servers()
