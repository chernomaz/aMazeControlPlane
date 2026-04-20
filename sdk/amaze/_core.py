"""Core environment + orchestrator registration for the amaze SDK.

Reads the NEMO-injected environment variables, registers with the
Orchestrator, and exposes the resulting identity + bearer token to the
rest of the SDK. All state is kept in module-level variables because the
SDK is intended to run as a single process per container — the NEMO
contract already guarantees a stable (agent_id, proxy) pair.
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
    a2a_token: str | None = None
    # Lock guards late assignment to a2a_token from the registration thread
    # while the FastAPI handlers may already be reading it on the event loop.
    # Python's GIL makes string attribute writes atomic, but an explicit lock
    # documents the race and makes rotation (Sprint 10+) straightforward.
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

    _config.proxy_url = os.environ.get("AMAZE_PROXY", "http://envoy:10000").rstrip("/")
    _config.orchestrator_url = os.environ.get(
        "AMAZE_ORCHESTRATOR_URL", "http://orchestrator:7000"
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
    """Background worker — register with the Orchestrator, poll until RUNNING.

    Stores the returned a2a_token on Config (atomic under _lock). Sets
    `on_ready` the moment the Orchestrator reports RUNNING, which unblocks
    whichever part of the SDK is waiting (typically `init()`'s main thread,
    if the user asked us to block until ready).
    """
    body = json.dumps(
        {
            "agent_id": _config.agent_id,
            "host": os.environ.get("AMAZE_CONTAINER_HOST", _config.agent_id),
            "chat_port": _config.chat_port,
            "a2a_port": _config.a2a_port,
        }
    ).encode()

    status = "PENDING"
    for attempt in range(30):
        try:
            req = urllib.request.Request(
                f"{_config.orchestrator_url}/agents/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read())
                tok = payload.get("a2a_token")
                if tok:
                    with _config._lock:
                        _config.a2a_token = tok
                status = payload.get("status", "PENDING")
                # Don't leak the raw bearer to stdout — log a redacted line.
                safe = {**payload, "a2a_token": "<redacted>"} if tok else payload
                print(f"[amaze {_config.agent_id}] registered: {safe}", flush=True)
                break
        except (urllib.error.URLError, ConnectionError) as e:
            print(
                f"[amaze {_config.agent_id}] register attempt {attempt+1} failed: {e}",
                flush=True,
            )
            time.sleep(2)
    else:
        # Hard fail — the container's compose restart policy (if set) should
        # rescue us. Matches docker/mcp_entrypoint.py's fail_fatal pattern.
        print(f"[amaze {_config.agent_id}] giving up on registration", flush=True)
        os._exit(1)

    if status == "RUNNING":
        on_ready.set()
        return

    # Poll until the admin pushes policy.
    while True:
        try:
            with urllib.request.urlopen(
                f"{_config.orchestrator_url}/agents/{_config.agent_id}/status",
                timeout=3,
            ) as resp:
                payload = json.loads(resp.read())
                if payload.get("status") == "RUNNING":
                    print(
                        f"[amaze {_config.agent_id}] policy received -> RUNNING",
                        flush=True,
                    )
                    on_ready.set()
                    return
        except Exception as e:
            print(f"[amaze {_config.agent_id}] status poll error: {e}", flush=True)
        time.sleep(1)
