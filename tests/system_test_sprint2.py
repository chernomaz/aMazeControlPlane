"""
system_test_sprint2.py — Sprint 2 system tests (A2A allow/deny enforcement)

Tests:
  ST-2.1  Allowed pair succeeds     : agent-a → agent-b → HTTP 200
  ST-2.2  Denied pair returns 403   : agent-a → agent-c → HTTP 403, reason `not-allowed`
  ST-2.3  Unknown caller returns 403: x-agent-id: unknown-agent → HTTP 403, reason `unknown-caller`

Prerequisites:
  - Envoy running on :10000
  - Policy Processor running on :50051 (Sprint 2 enforcer wired)

Run:
  /home/ubuntu/venv/bin/python tests/system_test_sprint2.py
"""
import sys
import os
import subprocess
import time
import signal
import socket
import uuid

import httpx

ENVOY_URL = "http://localhost:10000"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = "/home/ubuntu/venv/bin/python"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_failures = 0


def check(name: str, condition: bool, detail: str = ""):
    global _failures
    status = PASS if condition else FAIL
    print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        _failures += 1


def make_a2a_request(text: str = "hello") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "id": str(uuid.uuid4()),
        "params": {
            "id": str(uuid.uuid4()),
            "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
        },
    }


def start_agent(name: str) -> subprocess.Popen:
    env = os.environ.copy()
    return subprocess.Popen(
        [PYTHON, os.path.join(REPO, "agents", name, "main.py")],
        cwd=os.path.join(REPO, "agents"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def kill_port(port: int):
    result = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    for pid in result.stdout.strip().splitlines():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


# ─────────────────────────────────────────────────────────────────────────────

def test_st2_1():
    """ST-2.1  Allowed pair succeeds: agent-a → agent-b → HTTP 200."""
    print("\nST-2.1  Allowed pair succeeds")

    kill_port(9001)
    kill_port(9002)
    agent_b = start_agent("agent_b")
    agent_a = start_agent("agent_a")

    ready_b = wait_for_port(9002, timeout=12)
    ready_a = wait_for_port(9001, timeout=5)
    check("Agent B started", ready_b, "port 9002 listening")
    check("Agent A started", ready_a, "port 9001 listening")
    if not (ready_b and ready_a):
        agent_a.terminate()
        agent_b.terminate()
        return
    time.sleep(0.5)

    try:
        resp = httpx.post(
            ENVOY_URL,
            json=make_a2a_request("hello from ST-2.1"),
            headers={"x-agent-id": "agent-a", "Host": "agent-b"},
            timeout=10,
        )
        check("HTTP 200", resp.status_code == 200, f"got {resp.status_code}")
        body = resp.json()
        check("valid JSON-RPC response", "result" in body, str(body)[:80])
    except Exception as e:
        check("request succeeded", False, str(e))
    finally:
        agent_a.terminate()
        agent_b.terminate()


def test_st2_2():
    """ST-2.2  Denied pair returns 403: agent-a → agent-c → HTTP 403 reason=not-allowed.
    No upstream required — Policy Processor denies at ext_proc before routing.
    """
    print("\nST-2.2  Denied pair returns 403")

    try:
        resp = httpx.post(
            ENVOY_URL,
            json=make_a2a_request("hello agent-c"),
            headers={"x-agent-id": "agent-a", "Host": "agent-c"},
            timeout=5,
        )
        check("HTTP 403", resp.status_code == 403, f"got {resp.status_code}")
        try:
            body = resp.json()
        except Exception:
            body = {}
        check(
            "reason=not-allowed",
            body.get("reason") == "not-allowed",
            f"body={body}",
        )
    except Exception as e:
        check("request returned 403", False, str(e))


def test_st2_3():
    """ST-2.3  Unknown caller returns 403: x-agent-id=unknown-agent → HTTP 403 reason=unknown-caller."""
    print("\nST-2.3  Unknown caller returns 403")

    try:
        resp = httpx.post(
            ENVOY_URL,
            json=make_a2a_request("hello"),
            headers={"x-agent-id": "unknown-agent", "Host": "agent-b"},
            timeout=5,
        )
        check("HTTP 403", resp.status_code == 403, f"got {resp.status_code}")
        try:
            body = resp.json()
        except Exception:
            body = {}
        check(
            "reason=unknown-caller",
            body.get("reason") == "unknown-caller",
            f"body={body}",
        )
    except Exception as e:
        check("request returned 403", False, str(e))


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Sprint 2 System Tests (A2A allow/deny enforcement)")
    print("Envoy + Policy Processor must be running.")
    print("=" * 60)

    try:
        httpx.get("http://localhost:9901/ready", timeout=2)
    except Exception:
        print(f"\n{FAIL}  Envoy admin not reachable on :9901 — is Envoy running?")
        sys.exit(1)

    if not wait_for_port(50051, timeout=2):
        print(f"\n{FAIL}  Policy Processor not reachable on :50051")
        sys.exit(1)

    test_st2_1()
    test_st2_2()
    test_st2_3()

    print("\n" + "=" * 60)
    if _failures == 0:
        print(f"{PASS}  All Sprint 2 tests passed")
        sys.exit(0)
    else:
        print(f"{FAIL}  {_failures} test(s) failed")
        sys.exit(1)
