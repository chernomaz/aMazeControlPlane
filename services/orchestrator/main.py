"""
Orchestrator — passive registration + resolution service.

Endpoints:
  GET  /health                    — liveness + Redis ping + CA presence.
  POST /register                  — agent registers; returns bearer token.
  POST /register?kind=mcp         — MCP server registers its name/url/tools.
  GET  /resolve/mcp/{name}        — proxy asks where to forward an MCP call.
  GET  /resolve/agent/{agent_id}  — proxy asks where to forward an A2A call.

Redis keyspace (used by both orchestrator and proxy):
  session_token:{token}        STRING  → agent_id                   (24h TTL)
  session:{sid}:agent          STRING  → agent_id                   (24h TTL)
  mcp:{name}                   STRING  → json(url, tools)           (no TTL)
  agent_session:{aid}          STRING  → session_id                 (24h TTL)
  agent:{agent_id}:endpoint    STRING  → http://{host}:{port}       (24h TTL)

All mutations are atomic with respect to a single key (SET/SETEX). No
multi-key transactions are needed in step 1.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import secrets
import uuid
from typing import Any

import redis.asyncio as redis
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from services.orchestrator.routers import agents as agents_router
from services.orchestrator.routers import alerts as alerts_router
from services.orchestrator.routers import export as export_router
from services.orchestrator.routers import llms as llms_router
from services.orchestrator.routers import mcp as mcp_router
from services.orchestrator.routers import policy as policy_router
from services.orchestrator.routers import traces as traces_router
from services.proxy import policy_store

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
CONFIG_DIR = pathlib.Path(os.environ.get("CONFIG_DIR", "/app/config"))
PROXY_CA_PATH = os.environ.get(
    "PROXY_CA_PATH", "/opt/mitmproxy/mitmproxy-ca-cert.pem"
)

SESSION_TTL_SECONDS = 24 * 60 * 60


# --- Schemas --------------------------------------------------------------

class RegisterAgentRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    a2a_host: str = Field(default="")
    a2a_port: int = Field(default=9002)
    chat_host: str = Field(default="")
    chat_port: int = Field(default=8080)


class RegisterAgentResponse(BaseModel):
    session_id: str
    bearer_token: str
    agent_id: str


class RegisterMCPRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)


class ResolveMCPResponse(BaseModel):
    name: str
    url: str
    tools: list[str]


class ResolveAgentResponse(BaseModel):
    agent_id: str
    endpoint: str


# --- App lifecycle --------------------------------------------------------

@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan handler — replaces the deprecated @on_event hooks.

    Opens the Redis connection, seeds the MCP registry from YAML, yields
    control to request handling, then closes Redis on shutdown.
    """
    app.state.redis = redis.from_url(REDIS_URL, decode_responses=True)
    await _bootstrap_mcp_servers(app.state.redis)
    # S4-T2.2: seed Redis policy:{agent_id} from YAML for any agent_id not
    # yet present. Idempotent — never overwrites existing Redis values.
    written = await policy_store.bootstrap_from_yaml()
    logger.info("orchestrator up: redis=%s config=%s policies_seeded=%d",
                REDIS_URL, CONFIG_DIR, written)
    try:
        yield
    finally:
        await app.state.redis.aclose()


app = FastAPI(title="aMaze Orchestrator", version="0.1.0", lifespan=_lifespan)

# S4-T1.2 — read-only GUI endpoints. Each lives in its own router module;
# main.py keeps /health, /register and /resolve/* (Phase 2 will move them).
app.include_router(agents_router.router)
app.include_router(mcp_router.router)
app.include_router(llms_router.router)
app.include_router(traces_router.router)
app.include_router(policy_router.router)
app.include_router(alerts_router.router)
app.include_router(export_router.router)


async def _bootstrap_mcp_servers(r: redis.Redis) -> None:
    """Load mcp_servers.yaml and seed Redis with entries that don't exist.
    Self-registrations (live POST /register?kind=mcp) always win — we use
    SETNX so existing entries are preserved across restarts."""
    path = CONFIG_DIR / "mcp_servers.yaml"
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text()) or {}
    servers = (data.get("mcp_servers") or {})
    for name, spec in servers.items():
        payload = json.dumps({"url": spec["url"], "tools": spec.get("tools", [])})
        await r.setnx(f"mcp:{name}", payload)
    logger.info("bootstrapped %d mcp servers from yaml", len(servers))


# --- Endpoints ------------------------------------------------------------

@app.get("/ca.pem", response_class=PlainTextResponse)
async def get_ca_cert() -> PlainTextResponse:
    """Serve the mitmproxy CA certificate so remote agents can bootstrap
    TLS trust without a manual copy step.

    Remote agents call this before registration, write the cert to a temp
    file, and set SSL_CERT_FILE + REQUESTS_CA_BUNDLE automatically.
    Co-resident agents use the Docker volume mount instead and never call
    this endpoint.
    """
    try:
        cert = pathlib.Path(PROXY_CA_PATH).read_text()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="ca-not-found")
    except OSError as e:
        raise HTTPException(status_code=503, detail=f"ca-read-error: {e}") from e
    return PlainTextResponse(cert, media_type="application/x-pem-file")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness. Reports Redis ping status and mitmproxy CA presence so the
    ST-S1.1 boot-health check can verify the full stack in one probe."""
    redis_ok = False
    try:
        pong = await app.state.redis.ping()
        redis_ok = bool(pong)
    except Exception:  # noqa: BLE001 — diagnostic probe, don't leak details
        redis_ok = False

    ca_ok = os.path.exists(PROXY_CA_PATH)
    return {
        "status": "ok" if (redis_ok and ca_ok) else "degraded",
        "redis": redis_ok,
        "proxy_ca": ca_ok,
    }


@app.post("/register")
async def register(
    body: dict[str, Any],
    kind: str = Query(default="agent", pattern="^(agent|mcp)$"),
) -> dict[str, Any]:
    """
    Two shapes, discriminated by `?kind=`:
      kind=agent (default)  → body { agent_id }.  Returns session + bearer.
      kind=mcp              → body { name, url, tools }. 201, no token.

    Registration is always accepted. Enforcement happens at traffic time by
    the proxy addon, which looks up the agent's policy in policies.yaml.
    An agent registering with an agent_id that has no policy entry will see
    every request denied with `policy-not-found` (fail-closed).
    """
    if kind == "agent":
        req = RegisterAgentRequest.model_validate(body)
        return (await _register_agent(req)).model_dump()
    # kind == "mcp"
    req_mcp = RegisterMCPRequest.model_validate(body)
    await _register_mcp(req_mcp)
    return {"status": "registered", "name": req_mcp.name}


async def _register_agent(req: RegisterAgentRequest) -> RegisterAgentResponse:
    r: redis.Redis = app.state.redis
    session_id = str(uuid.uuid4())
    bearer_token = secrets.token_urlsafe(32)

    pipe = r.pipeline()
    pipe.setex(f"session_token:{bearer_token}", SESSION_TTL_SECONDS, req.agent_id)
    pipe.setex(f"session:{session_id}:agent", SESSION_TTL_SECONDS, req.agent_id)
    pipe.setex(f"agent_session:{req.agent_id}", SESSION_TTL_SECONDS, session_id)
    if req.a2a_host:
        endpoint = f"http://{req.a2a_host}:{req.a2a_port}"
        pipe.setex(f"agent:{req.agent_id}:endpoint", SESSION_TTL_SECONDS, endpoint)
    if req.chat_host:
        chat_endpoint = f"http://{req.chat_host}:{req.chat_port}"
        pipe.setex(f"agent:{req.agent_id}:chat_endpoint", SESSION_TTL_SECONDS, chat_endpoint)
    # Default to pending — operator must explicitly approve via POST /agents/{id}/approve.
    # SETNX preserves a pre-existing approval decision (operator approved before agent registered).
    pipe.setnx(f"agent:{req.agent_id}:approved", "false")
    await pipe.execute()

    logger.info("registered agent_id=%s session=%s", req.agent_id, session_id)
    return RegisterAgentResponse(
        session_id=session_id,
        bearer_token=bearer_token,
        agent_id=req.agent_id,
    )


async def _register_mcp(req: RegisterMCPRequest) -> None:
    r: redis.Redis = app.state.redis
    payload = json.dumps({"url": req.url, "tools": list(req.tools)})
    await r.set(f"mcp:{req.name}", payload)
    logger.info("registered mcp name=%s url=%s tools=%d",
                req.name, req.url, len(req.tools))


@app.get("/resolve/mcp/{name}", response_model=ResolveMCPResponse)
async def resolve_mcp(name: str) -> ResolveMCPResponse:
    try:
        raw = await app.state.redis.get(f"mcp:{name}")
    except redis.RedisError as e:
        logger.error("redis unreachable during resolve_mcp(%s): %s", name, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    if not raw:
        raise HTTPException(status_code=404, detail="mcp-not-registered")
    data = json.loads(raw)
    return ResolveMCPResponse(name=name, url=data["url"], tools=data.get("tools", []))


@app.get("/resolve/agent/{agent_id}", response_model=ResolveAgentResponse)
async def resolve_agent(agent_id: str) -> ResolveAgentResponse:
    try:
        endpoint = await app.state.redis.get(f"agent:{agent_id}:endpoint")
    except redis.RedisError as e:
        logger.error("redis unreachable during resolve_agent(%s): %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    if not endpoint:
        raise HTTPException(status_code=404, detail="agent-not-registered")
    return ResolveAgentResponse(agent_id=agent_id, endpoint=endpoint)
