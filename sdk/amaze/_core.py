"""Core environment + orchestrator registration for the amaze SDK.

Reads the orchestrator-injected environment variables, registers with the
Orchestrator, and exposes the resulting identity + bearer token to the
rest of the SDK. All state is kept in module-level variables because the
SDK is intended to run as a single process per container — one agent_id,
one bearer, one proxy.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Config:
    """Resolved runtime configuration. Populated by `load()`."""

    agent_id: str = ""
    proxy_url: str = ""
    orchestrator_url: str = ""
    chat_port: int = 8080
    a2a_port: int = 9002
    session_id: str | None = None
    bearer_token: str | None = None
    # Lock guards late assignment to bearer_token from the registration
    # thread while FastAPI handlers may already be reading it on the
    # event loop.
    _lock: threading.Lock = field(default_factory=threading.Lock)


_config = Config()


def load(agent_id_override: str | None = None) -> Config:
    """Populate the module-level Config from env vars. Idempotent.

    `agent_id_override` lets users pass an explicit id to `amaze.init()`;
    otherwise `AMAZE_AGENT_ID` is required.
    """
    if _config.agent_id:
        return _config

    _config.agent_id = (agent_id_override or os.environ.get("AMAZE_AGENT_ID") or "").strip()
    if not _config.agent_id:
        raise RuntimeError(
            "amaze: agent_id is required (pass to init() or set AMAZE_AGENT_ID)"
        )

    _config.proxy_url = os.environ.get("AMAZE_PROXY_URL", "http://amaze:8080").rstrip("/")
    _config.orchestrator_url = os.environ.get(
        "AMAZE_ORCHESTRATOR_URL", "http://amaze:8001"
    ).rstrip("/")
    _config.chat_port = int(os.environ.get("AMAZE_CHAT_PORT", "8080"))
    _config.a2a_port = int(os.environ.get("AMAZE_A2A_PORT", "9002"))
    return _config


def cfg() -> Config:
    """Return the (loaded) Config. Must be called after `load()`."""
    if not _config.agent_id:
        raise RuntimeError("amaze: SDK not initialised — call amaze.init() first")
    return _config


def register_and_wait(on_ready: threading.Event) -> None:
    """Background worker — register with the Orchestrator, set ready.

    The orchestrator's /register endpoint is single-shot: one POST returns
    the session_id + bearer_token. There is no RUNNING-vs-PENDING polling
    in the new control plane; the agent is ready the moment it has a
    bearer. This function retries network failures only (transient host
    unreachable during compose startup), not HTTP error responses.
    """
    body = json.dumps({"agent_id": _config.agent_id}).encode()

    for attempt in range(30):
        try:
            req = urllib.request.Request(
                f"{_config.orchestrator_url}/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read())
                tok = payload.get("bearer_token")
                sid = payload.get("session_id")
                if not tok or not sid:
                    raise RuntimeError(f"orchestrator returned incomplete payload: {payload}")
                with _config._lock:
                    _config.bearer_token = tok
                    _config.session_id = sid
                safe = {**payload, "bearer_token": "<redacted>"}
                print(f"[amaze {_config.agent_id}] registered: {safe}", flush=True)
                on_ready.set()
                return
        except (urllib.error.URLError, ConnectionError) as e:
            print(
                f"[amaze {_config.agent_id}] register attempt {attempt+1} failed: {e}",
                flush=True,
            )
            time.sleep(2)

    # Hard fail — the container's compose restart policy (if set) should
    # rescue us.
    print(f"[amaze {_config.agent_id}] giving up on registration", flush=True)
    os._exit(1)
