"""NEMO MCP-server container entrypoint — Phase 8B Slice 1.

An MCP server is a passive capability endpoint (no per-caller lifecycle),
so the container only needs to:

  1. Register itself with the Orchestrator exactly once via POST /mcp/register.
     The orchestrator is idempotent — a restarted container just overwrites
     its prior record.
  2. Run the fastmcp server from examples/mcp_server/ on 0.0.0.0.

No policy wait loop, no PENDING/RUNNING — policies are attached to calling
agents, not to MCP servers.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request


MCP_ID = os.environ["AMAZE_MCP_ID"]
CONTAINER_HOST = os.environ.get("AMAZE_CONTAINER_HOST", MCP_ID)
MCP_PORT = int(os.environ.get("AMAZE_MCP_PORT", "8000"))
ORCHESTRATOR_URL = os.environ["AMAZE_ORCHESTRATOR_URL"].rstrip("/")


def wait_port_open(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll a TCP socket until it accepts a connection or the budget runs out.

    We register only after the MCP listener is actually bound so callers that
    resolve `mcp_id → host:port` from the registry never hit a refused-connect
    window during cold boot.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def fail_fatal(msg: str) -> None:
    """Print a fatal message and kill the whole process (from any thread).

    Registration failure must not leave an orphaned MCP serving on :8000 that
    the orchestrator has no record of. Exiting lets compose's restart policy
    rescue us instead of leaking a zombie.
    """
    print(f"[nemo-mcp {MCP_ID}] FATAL: {msg}", flush=True)
    os._exit(1)


def register_once() -> None:
    """Wait for the local MCP socket, then POST /mcp/register exactly once.

    Retries only on transport-level failure and 5xx. A 4xx means the payload
    itself is bad — no amount of retrying will fix it, so we bail immediately.
    """
    if not wait_port_open("127.0.0.1", MCP_PORT):
        fail_fatal(f"mcp port {MCP_PORT} never came up")

    body = json.dumps(
        {"mcp_id": MCP_ID, "host": CONTAINER_HOST, "port": MCP_PORT}
    ).encode()
    for attempt in range(30):
        try:
            req = urllib.request.Request(
                f"{ORCHESTRATOR_URL}/mcp/register",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read())
                print(f"[nemo-mcp {MCP_ID}] registered: {payload}", flush=True)
                return
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                fail_fatal(f"register rejected {e.code} — payload bad")
            print(
                f"[nemo-mcp {MCP_ID}] register attempt {attempt+1} got {e.code}",
                flush=True,
            )
            time.sleep(2)
        except (urllib.error.URLError, ConnectionError) as e:
            print(
                f"[nemo-mcp {MCP_ID}] register attempt {attempt+1} failed: {e}",
                flush=True,
            )
            time.sleep(2)
    fail_fatal("giving up on registration after 30 attempts")


def run_server() -> None:
    # examples/mcp_server/server.py executes load_tools() at import time;
    # importing it here yields a ready-to-serve FastMCP instance.
    sys.path.insert(0, "/app/mcp_server")
    from server import mcp  # type: ignore[import-not-found]

    print(
        f"[nemo-mcp {MCP_ID}] starting mcp on 0.0.0.0:{MCP_PORT}",
        flush=True,
    )
    mcp.run(transport="streamable-http", host="0.0.0.0", port=MCP_PORT)


if __name__ == "__main__":
    threading.Thread(target=register_once, daemon=True).start()
    run_server()
