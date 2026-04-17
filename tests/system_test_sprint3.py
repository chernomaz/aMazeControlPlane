"""
system_test_sprint3.py — Sprint 3 system tests (MCP enforcement)

Tests:
  ST-3.1  Allowed tool passes          : agent-a-mcp → echo → HTTP 200, result contains echo text
  ST-3.2  Blocked tool returns 403     : agent-a-mcp → dangerous_tool → HTTP 403, reason tool-not-allowed
  ST-3.3  Unknown MCP server returns 403: Host: unlisted-mcp → HTTP 403, reason mcp-server-not-allowed

Prerequisites:
  - Envoy running on :10000  (admin :9901)
  - Policy Processor running on :50051
  - MCP Server running on :9003

Run:
  /home/ubuntu/venv/bin/python tests/system_test_sprint3.py
"""
import sys
import asyncio
import socket
import time

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ENVOY_URL = "http://localhost:10000"
MCP_PATH = "/mcp"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_failures = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    status = PASS if condition else FAIL
    print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        _failures += 1


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def mcp_headers(agent_id: str, server: str) -> dict:
    return {
        "x-agent-id": agent_id,
        "Host": server,
        "Content-Type": "application/json",
    }


# ─────────────────────────────────────────────────────────────────────────────

async def test_st3_1_async() -> None:
    """ST-3.1: Allowed tool passes — echo returns its input through Envoy."""
    print("\nST-3.1  Allowed tool passes (echo)")
    transport = StreamableHttpTransport(
        url=f"{ENVOY_URL}{MCP_PATH}",
        headers={
            "x-agent-id": "agent-a-mcp",
            "host": "mcp-server",
        },
    )
    result = None
    try:
        async with Client(transport) as client:
            result = await client.call_tool("echo", {"text": "hello-sprint3"})
    except Exception as exc:
        check("tool call succeeded", False, str(exc))
        check("response contains echo text", False)
        return

    result_text = str(result)
    check("tool call succeeded", True)
    check(
        "response contains echo text",
        "hello-sprint3" in result_text,
        f"got: {result_text[:120]}",
    )


def test_st3_1() -> None:
    asyncio.run(test_st3_1_async())


def test_st3_2() -> None:
    """ST-3.2: Blocked tool returns 403 — dangerous_tool denied before MCP server."""
    print("\nST-3.2  Blocked tool returns 403 (dangerous_tool)")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "dangerous_tool", "arguments": {}},
    }
    try:
        resp = httpx.post(
            f"{ENVOY_URL}{MCP_PATH}",
            headers=mcp_headers("agent-a-mcp", "mcp-server"),
            json=payload,
            timeout=5,
        )
        check("HTTP 403", resp.status_code == 403, f"got {resp.status_code}")
        try:
            body = resp.json()
        except Exception:
            body = {}
        check(
            "reason=tool-not-allowed",
            body.get("reason") == "tool-not-allowed",
            f"body={body}",
        )
    except Exception as exc:
        check("request returned 403", False, str(exc))
        check("reason=tool-not-allowed", False)


def test_st3_3() -> None:
    """ST-3.3: Unknown MCP server returns 403."""
    print("\nST-3.3  Unknown MCP server returns 403 (unlisted-mcp)")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hi"}},
    }
    try:
        resp = httpx.post(
            f"{ENVOY_URL}{MCP_PATH}",
            headers=mcp_headers("agent-a-mcp", "unlisted-mcp"),
            json=payload,
            timeout=5,
        )
        check("HTTP 403", resp.status_code == 403, f"got {resp.status_code}")
        try:
            body = resp.json()
        except Exception:
            body = {}
        check(
            "reason=mcp-server-not-allowed",
            body.get("reason") == "mcp-server-not-allowed",
            f"body={body}",
        )
    except Exception as exc:
        check("request returned 403", False, str(exc))
        check("reason=mcp-server-not-allowed", False)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Sprint 3 System Tests (MCP enforcement)")
    print("Envoy + Policy Processor + MCP Server must be running.")
    print("=" * 60)

    try:
        httpx.get("http://localhost:9901/ready", timeout=2)
    except Exception:
        print(f"\n{FAIL}  Envoy admin not reachable on :9901 — is Envoy running?")
        sys.exit(1)

    if not wait_for_port(50051, timeout=2):
        print(f"\n{FAIL}  Policy Processor not reachable on :50051")
        sys.exit(1)

    if not wait_for_port(9003, timeout=2):
        print(f"\n{FAIL}  MCP Server not reachable on :9003")
        sys.exit(1)

    test_st3_1()
    test_st3_2()
    test_st3_3()

    print("\n" + "=" * 60)
    if _failures == 0:
        print(f"{PASS}  All Sprint 3 tests passed")
        sys.exit(0)
    else:
        print(f"{FAIL}  {_failures} test(s) failed")
        sys.exit(1)
