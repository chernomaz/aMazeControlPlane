"""
system_test_sprint4.py — Sprint 4 system tests (limits enforcement)

Tests:
  ST-4.1  Rate limit triggers      : 2 MCP calls pass, 3rd → 403 rate-limit-exceeded;
                                     after 3-s window resets → passes again
  ST-4.2  Oversized request denied : A2A body > 256 bytes → 403 request-too-large
  ST-4.3  Call count exhaustion    : 2 MCP echo calls pass, 3rd → 403 call-limit-exceeded

Prerequisites:
  - Envoy running on :10000  (admin :9901)
  - Policy Processor running on :50051
  - MCP Server running on :9003

Run:
  /home/ubuntu/venv/bin/python tests/system_test_sprint4.py
"""
import sys
import socket
import time
import uuid

import httpx

ENVOY_URL = "http://localhost:10000"
MCP_PATH = "/mcp"
MCP_ACCEPT = "application/json, text/event-stream"

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


# ── Request builders ──────────────────────────────────────────────────────────

def mcp_tool_call(agent_id: str, server: str, tool: str, args: dict = None) -> httpx.Response:
    return httpx.post(
        f"{ENVOY_URL}{MCP_PATH}",
        headers={
            "x-agent-id": agent_id,
            "Host": server,
            "Content-Type": "application/json",
            "Accept": MCP_ACCEPT,
        },
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        },
        timeout=5,
    )


def a2a_send(agent_id: str, target: str, text: str) -> httpx.Response:
    return httpx.post(
        ENVOY_URL,
        headers={"x-agent-id": agent_id, "Host": target},
        json={
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/send",
            "params": {
                "id": str(uuid.uuid4()),
                "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
            },
        },
        timeout=5,
    )


def is_denied(resp: httpx.Response, reason: str) -> bool:
    """True iff the response is an ext_proc denial with the given reason code."""
    try:
        return resp.status_code == 403 and resp.json().get("reason") == reason
    except Exception as exc:
        print(f"  [warn] is_denied parse error: {exc}")
        return False


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_st4_1() -> None:
    """ST-4.1: Rate limit — agent-a-mcp limited to 2 tools/call per 3-second window."""
    print("\nST-4.1  Rate limit triggers (agent-a-mcp, 2 req / 3 s)")

    try:
        r1 = mcp_tool_call("agent-a-mcp", "mcp-server", "echo", {"text": "r1"})
        check(
            "request 1 passes (under limit)",
            not is_denied(r1, "rate-limit-exceeded"),
            f"status={r1.status_code}",
        )

        r2 = mcp_tool_call("agent-a-mcp", "mcp-server", "echo", {"text": "r2"})
        check(
            "request 2 passes (at limit)",
            not is_denied(r2, "rate-limit-exceeded"),
            f"status={r2.status_code}",
        )

        r3 = mcp_tool_call("agent-a-mcp", "mcp-server", "echo", {"text": "r3"})
        check("request 3 → HTTP 403", r3.status_code == 403, f"got {r3.status_code}")
        check(
            "reason=rate-limit-exceeded",
            r3.json().get("reason") == "rate-limit-exceeded",
            f"body={r3.text[:80]}",
        )

        print("  (waiting 4 s for 3-second window to reset...)")
        time.sleep(4)

        r4 = mcp_tool_call("agent-a-mcp", "mcp-server", "echo", {"text": "r4"})
        check(
            "request 4 passes after window reset",
            not is_denied(r4, "rate-limit-exceeded"),
            f"status={r4.status_code}",
        )
    except Exception as exc:
        check("ST-4.1 completed without exception", False, str(exc))


def test_st4_2() -> None:
    """ST-4.2: Oversized request — agent-a body > 256 bytes → 403 request-too-large."""
    print("\nST-4.2  Oversized request denied (agent-a, limit 256 bytes)")

    # Build a body that exceeds 256 bytes
    large_text = "X" * 300
    try:
        resp = a2a_send("agent-a", "agent-b", large_text)
        body_size = len(resp.request.content)
        check("HTTP 403", resp.status_code == 403, f"got {resp.status_code}")
        check(
            "reason=request-too-large",
            resp.json().get("reason") == "request-too-large",
            f"body={resp.text[:80]}",
        )
        check("request body was > 256 bytes", body_size > 256, f"body_size={body_size}")
    except Exception as exc:
        check("ST-4.2 completed without exception", False, str(exc))


def test_st4_3() -> None:
    """ST-4.3: Call count exhaustion — agent-a-mcp-counter limited to 2 echo calls."""
    print("\nST-4.3  Call count exhaustion (agent-a-mcp-counter, 2 echo calls)")

    try:
        c1 = mcp_tool_call("agent-a-mcp-counter", "mcp-server", "echo", {"text": "c1"})
        check(
            "call 1 passes",
            not is_denied(c1, "call-limit-exceeded"),
            f"status={c1.status_code}",
        )

        c2 = mcp_tool_call("agent-a-mcp-counter", "mcp-server", "echo", {"text": "c2"})
        check(
            "call 2 passes",
            not is_denied(c2, "call-limit-exceeded"),
            f"status={c2.status_code}",
        )

        c3 = mcp_tool_call("agent-a-mcp-counter", "mcp-server", "echo", {"text": "c3"})
        check("call 3 → HTTP 403", c3.status_code == 403, f"got {c3.status_code}")
        check(
            "reason=call-limit-exceeded",
            c3.json().get("reason") == "call-limit-exceeded",
            f"body={c3.text[:80]}",
        )
    except Exception as exc:
        check("ST-4.3 completed without exception", False, str(exc))


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Sprint 4 System Tests (limits enforcement)")
    print("Envoy + Policy Processor + MCP Server must be running.")
    print("=" * 60)

    try:
        httpx.get("http://localhost:9901/ready", timeout=2)
    except Exception:
        print(f"\n{FAIL}  Envoy admin not reachable on :9901")
        sys.exit(1)

    if not wait_for_port(50051, timeout=2):
        print(f"\n{FAIL}  Policy Processor not reachable on :50051")
        sys.exit(1)

    if not wait_for_port(9003, timeout=2):
        print(f"\n{FAIL}  MCP Server not reachable on :9003")
        sys.exit(1)

    test_st4_1()
    test_st4_2()
    test_st4_3()

    print("\n" + "=" * 60)
    if _failures == 0:
        print(f"{PASS}  All Sprint 4 tests passed")
        sys.exit(0)
    else:
        print(f"{FAIL}  {_failures} test(s) failed")
        sys.exit(1)
