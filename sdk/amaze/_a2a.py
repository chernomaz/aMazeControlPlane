"""A2A transport — outbound JSON-RPC client + inbound FastAPI server factory.

Hides the JSON-RPC envelope, bearer injection, and Envoy routing from
the agent author. Public SDK exposes only `send_message_to_agent(target,
message) -> str`; inbound traffic is delivered to the user-defined
`receive_message_from_agent(agent, message)` via the FastAPI app built
here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from http import HTTPStatus
from typing import Any, Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import _core, _handlers


# ── outbound client ─────────────────────────────────────────────────────────

class SendError(RuntimeError):
    """Raised by `send_message_to_agent` when the Envoy round-trip returns a
    non-2xx status (either policy deny or partner error). The exception
    carries the HTTP status + the JSON-RPC error / deny reason so callers
    can distinguish retryable from fatal failures."""

    def __init__(self, status_code: int, reason: str | None, body: Any) -> None:
        self.status_code = status_code
        self.reason = reason
        self.body = body
        super().__init__(f"send failed: status={status_code} reason={reason}")


def send_sync(target: str, message: Any) -> Any:
    """Blocking send — safe from sync handlers AND async handlers.

    When called from inside a running event loop we can't use
    `asyncio.run` (it would fail with "event loop already running"); we
    defer to a one-shot thread that spins up its own loop. Async authors
    who care about not blocking their handler should use
    `await asyncio.to_thread(amaze.send_message_to_agent, target, msg)`
    — that offloads the blocking hop to the default threadpool instead
    of pinning a new one per call.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(send_async(target, message))
    # Inside a running loop — fork a dedicated thread so we don't collide.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(send_async(target, message))).result()


def _encode_part(value: Any) -> dict:
    """Encode a Python value as an A2A message part.

    Strings use `type=text` (backward-compatible). Any other JSON-serializable
    value uses `type=json` with the value JSON-encoded in the `text` field.
    """
    if isinstance(value, str):
        return {"type": "text", "text": value}
    return {"type": "json", "text": json.dumps(value)}


def _decode_part(part: dict) -> Any:
    """Decode an A2A message part back to a Python value.

    `type=json` parts are JSON-decoded; anything else is returned as a string.
    """
    if part.get("type") == "json":
        raw = part.get("text", "")
        try:
            return json.loads(raw) if raw else None
        except (ValueError, TypeError):
            return raw
    return part.get("text", "") or ""


async def send_async(target: str, message: Any) -> Any:
    """Async send — used by async handlers; `send_sync` wraps it in asyncio.run."""
    c = _core.cfg()
    url = f"http://{target}:{c.a2a_port}/"
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tasks/send",
        "params": {
            "id": "task-sdk-1",
            # Intentionally no `from` field — the receiver reads caller
            # identity from the proxy-injected `x-amaze-caller` header.
            # Adding `from` here would be ignored but also misleading.
            "message": {"role": "user", "parts": [_encode_part(message)]},
        },
    }
    headers = {"Content-Type": "application/json"}
    # A2A identity travels as `X-Amaze-Bearer` — a dedicated header so it
    # never collides with the upstream's own `Authorization` (OpenAI API
    # key, Anthropic x-api-key, etc.). The bearer is populated by
    # _core.register_and_wait — if it isn't set yet we fail loudly.
    with c._lock:
        token = c.bearer_token
    if not token:
        raise SendError(0, "no-bearer", "SDK registered without a bearer token")
    headers["X-Amaze-Bearer"] = token

    async with httpx.AsyncClient(timeout=30, proxy=c.proxy_url) as client:
        resp = await client.post(url, json=payload, headers=headers)

    # Structured deny bodies from the proxy:  {"error":"denied","reason":"..."}
    try:
        body = resp.json()
    except ValueError:
        body = resp.text

    if resp.status_code != 200:
        reason = None
        if isinstance(body, dict):
            reason = body.get("reason") or body.get("error")
        raise SendError(resp.status_code, reason, body)

    # Unwrap the A2A JSON-RPC envelope.
    if not isinstance(body, dict):
        raise SendError(200, "malformed-response", body)
    if "error" in body:
        err = body["error"] or {}
        raise SendError(200, err.get("message") or "rpc-error", body)
    result = body.get("result") or {}
    artifacts = result.get("artifacts") or []
    if artifacts:
        parts = artifacts[0].get("parts") or []
        if parts:
            return _decode_part(parts[0])
    return ""


# ── inbound server ──────────────────────────────────────────────────────────

def _chat_app(ready: threading.Event) -> FastAPI:
    """FastAPI app for the user-facing chat port (default 8080).

    Accepts `POST /chat {"message": "..."}` and dispatches to
    `receive_message_from_user`. Also exposes `GET /healthz` so the
    compose readiness probes have a stable endpoint.
    """
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        status = "RUNNING" if ready.is_set() else "PENDING"
        return {"status": status, "agent_id": _core.cfg().agent_id}

    @app.post("/chat")
    async def chat(req: Request) -> JSONResponse:
        if not ready.is_set():
            return JSONResponse(
                {"error": "agent-not-ready"},
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        try:
            body = await req.json()
        except Exception:
            return JSONResponse(
                {"error": "invalid-json"},
                status_code=HTTPStatus.BAD_REQUEST,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body-must-be-object"},
                status_code=HTTPStatus.BAD_REQUEST,
            )
        msg = body.get("message", "")
        try:
            reply = await _handlers.call_user_handler(msg)
        except _handlers.HandlerMissing as e:
            return JSONResponse(
                {"error": "handler-missing", "detail": str(e)},
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return JSONResponse({"reply": reply})

    return app


def _a2a_app(ready: threading.Event) -> FastAPI:
    """FastAPI app for the A2A ingress port (default 9002).

    Accepts JSON-RPC 2.0 `tasks/send` and dispatches to
    `receive_message_from_agent(caller_id, message)`.

    Caller identity — security contract + limits (Sprint 9):
    -------------------------------------------------------
    The bearer token identifies the sender to ext_proc; ext_proc resolves
    it against the tokens registry and, on A2A allow, injects the result
    as `x-amaze-caller` on the upstream request (via the OVERWRITE
    mutation action, so any client-supplied value is replaced). We read
    ONLY that trusted header here and ignore `params.from` in the body.

    THREAT MODEL CAVEAT — the trust placed in `x-amaze-caller` holds
    ONLY while every peer that can reach this port is routed through
    Envoy. On a Docker compose bridge network (the default deployment)
    any co-located container can curl `http://<agent>:9002/` directly,
    bypassing ext_proc, and attach its own forged `x-amaze-caller`. The
    SDK cannot distinguish "ext_proc injected this" from "a peer on the
    same network set this" — so a compromised peer defeats the
    authentication. Mitigation options, in order of effort:

      1. Deploy with a network topology where `:9002` is reachable only
         from Envoy (per-container firewall / Kubernetes NetworkPolicy).
      2. Add an HMAC signature field alongside `x-amaze-caller` using a
         secret shared between ext_proc and the SDK at container launch.
      3. mTLS between Envoy and agents (cert-pinned origin proof).

    Sprint 10+ is scoped to pick one of (2)/(3). Until then, treat
    caller_id as "authenticated against Envoy-mediated traffic only"
    and document per-deployment if the network is not segmented.

    The dispatcher refuses to call the handler when the trusted header
    is missing entirely — an explicit 401 with a JSON-RPC envelope so
    callers (sync or async) can tell configuration failures apart from
    policy denies.
    """
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        status = "RUNNING" if ready.is_set() else "PENDING"
        return {"status": status, "agent_id": _core.cfg().agent_id}

    @app.post("/")
    async def a2a(req: Request) -> JSONResponse:
        if not ready.is_set():
            return JSONResponse(
                {"error": "agent-not-ready"},
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        # Guard body parsing — a malformed / non-JSON body would otherwise
        # escape as a plain 500 from FastAPI's default handler, breaking
        # the JSON-RPC contract that senders rely on to surface errors.
        try:
            body = await req.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}},
                status_code=HTTPStatus.BAD_REQUEST,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "invalid request: body must be a JSON object"}},
                status_code=HTTPStatus.BAD_REQUEST,
            )
        rpc_id = body.get("id", "0")
        method = body.get("method", "")
        if method != "tasks/send":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": "method not supported"},
                }
            )

        params = body.get("params") or {}
        message = params.get("message") or {}
        parts = message.get("parts") or []
        text = _decode_part(parts[0]) if parts else ""
        # Read caller id ONLY from the ext_proc-injected header. `params.from`
        # is sender-controllable and would be spoofable — ignored by design.
        # Distinguish "absent" (request bypassed ext_proc; config failure)
        # from "empty" (ext_proc processed it but injected nothing — should
        # never happen, points at a processor bug) so operators can tell
        # infra misconfig apart from code regressions in the logs.
        caller_raw = req.headers.get("x-amaze-caller")
        if caller_raw is None:
            detail = "caller-id-missing: request did not pass through ext_proc"
        elif caller_raw == "":
            detail = "caller-id-empty: ext_proc injected an empty header (processor config bug)"
        else:
            caller_raw = caller_raw.strip()
            detail = None if caller_raw else (
                "caller-id-blank: ext_proc injected whitespace (processor config bug)"
            )
        if detail is not None:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32001, "message": detail},
                },
                # 401 Unauthorized — semantically "the request lacks valid
                # authentication credentials" (the injected caller id IS the
                # authentication). Deliberately not 5xx: clients that retry
                # on 5xx would hammer a broken path for no reason.
                status_code=HTTPStatus.UNAUTHORIZED,
            )
        caller_id = caller_raw

        try:
            reply = await _handlers.call_agent_handler(caller_id, text)
        except _handlers.HandlerMissing as e:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32000, "message": f"handler-missing: {e}"},
                }
            )

        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "id": params.get("id", rpc_id),
                    "status": {"state": "completed"},
                    "artifacts": [
                        {"parts": [_encode_part(reply)]}
                    ],
                },
            }
        )

    return app


def start_server(
    block: bool = True,
    on_startup: "Callable[[], Any] | None" = None,
) -> None:
    """Start both FastAPI servers + kick off registration in the background.

    When `block=True` (the default in `amaze.init()`), this never returns —
    the process's job is to serve requests forever. `block=False` is
    intended only for tests that want to tear the server down manually.

    `on_startup` is an optional async callable that runs once, immediately
    after registration completes (i.e. after the bearer token is received
    and the `ready` event fires). Use it to pre-build LangChain agents,
    fetch MCP tools, or warm any other async resource before the first
    request arrives. Failures are logged but do not crash the agent.
    """
    c = _core.cfg()
    ready = threading.Event()

    async def _run_startup_hook() -> None:
        # Wait for the registration thread to set `ready`, then run the hook
        # in the async event loop so it can freely await coroutines (e.g.
        # MCP get_tools). asyncio.to_thread bridges the threading.Event
        # into the async world without busy-waiting.
        await asyncio.to_thread(ready.wait)
        if on_startup is None:
            return
        try:
            await on_startup()
        except Exception as exc:  # noqa: BLE001 — non-fatal; log and continue
            print(f"[amaze {c.agent_id}] on_startup hook failed: {exc}", flush=True)

    async def _serve() -> None:
        chat_config = uvicorn.Config(
            _chat_app(ready), host="0.0.0.0", port=c.chat_port, log_level="warning"
        )
        a2a_config = uvicorn.Config(
            _a2a_app(ready), host="0.0.0.0", port=c.a2a_port, log_level="warning"
        )
        chat_server = uvicorn.Server(chat_config)
        a2a_server = uvicorn.Server(a2a_config)

        chat_task = asyncio.create_task(chat_server.serve())
        a2a_task = asyncio.create_task(a2a_server.serve())
        startup_task = asyncio.create_task(_run_startup_hook())

        # Don't register until both sockets are accepting — otherwise the
        # status can flip to RUNNING while the A2A port is still binding,
        # and an incoming A2A hits Envoy → `no_healthy_upstream`.
        while not (chat_server.started and a2a_server.started):
            await asyncio.sleep(0.05)
        threading.Thread(
            target=_core.register_and_wait, args=(ready,), daemon=True
        ).start()

        await asyncio.gather(chat_task, a2a_task, startup_task)

    if block:
        asyncio.run(_serve())
    else:
        # Spawn in a background thread so tests retain control. Not used
        # in production; keep simple.
        def _runner() -> None:
            asyncio.run(_serve())

        threading.Thread(target=_runner, daemon=True).start()
