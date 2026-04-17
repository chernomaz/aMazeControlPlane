"""
system_test_sprint1.py — Sprint 1 system tests

Tests:
  ST-1.1  Pass-through sanity: valid A2A request → HTTP 200, policy processor logs PASS
  ST-1.2  Fail-closed: policy processor down → Envoy returns non-200

Prerequisites:
  - Envoy running on :10000 (Docker --network=host)
  - Agent B running on :9002
  - Policy Processor running on :50051 (for ST-1.1)

Run:
  /home/ubuntu/venv/bin/python tests/system_test_sprint1.py
"""
import sys
import os
import json
import subprocess
import time
import signal
import uuid

import httpx

ENVOY_URL = "http://localhost:10000"
POLICY_PROCESSOR_PORT = 50051
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


def start_agent(name: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    return subprocess.Popen(
        [PYTHON, os.path.join(REPO, "examples", "agents", name, "main.py")],
        cwd=os.path.join(REPO, "examples", "agents"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_agent_b() -> subprocess.Popen:
    return start_agent("agent_b", 9002)


def start_policy_processor() -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO}:{os.path.join(REPO, 'policy_processor', 'proto')}"
    return subprocess.Popen(
        [PYTHON, os.path.join(REPO, "policy_processor", "server.py")],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_policy_processor(pp: subprocess.Popen):
    pp.send_signal(signal.SIGTERM)
    pp.wait(timeout=3)


def kill_port(port: int):
    result = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    for pid in result.stdout.strip().splitlines():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


# ─────────────────────────────────────────────────────────────────────────────

def wait_for_port(port: int, timeout: float = 10.0):
    """Poll until a TCP port is accepting connections."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def test_st1_1():
    """ST-1.1 Pass-through sanity.
    Uses the already-running PP + Envoy; only manages Agent B.
    """
    print("\nST-1.1  Pass-through sanity")

    kill_port(9001)
    kill_port(9002)
    agent_b = start_agent_b()
    agent_a = start_agent("agent_a", 9001)

    # Wait for both agents (CrewAI in Agent B takes ~4-5 s)
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
            json=make_a2a_request("hello from system test"),
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


def test_st1_2():
    """ST-1.2 Fail-closed: policy processor down → non-200.
    Stops the running PP, sends a request, then restarts PP.
    """
    print("\nST-1.2  Fail-closed (policy processor down)")

    # Stop PP temporarily
    kill_port(50051)
    time.sleep(0.5)

    kill_port(9001)
    kill_port(9002)
    agent_b = start_agent_b()
    agent_a = start_agent("agent_a", 9001)
    wait_for_port(9002, timeout=12)
    wait_for_port(9001, timeout=5)
    time.sleep(0.3)

    try:
        resp = httpx.post(
            ENVOY_URL,
            json=make_a2a_request("hello"),
            headers={"x-agent-id": "agent-a", "Host": "agent-b"},
            timeout=8,
        )
        check("non-200 when PP is down", resp.status_code != 200,
              f"got {resp.status_code} (expected 500 or 503)")
    except httpx.RequestError as e:
        check("connection failed (fail-closed)", True, str(e))
    except Exception as e:
        check("request failed", False, str(e))
    finally:
        agent_a.terminate()
        agent_b.terminate()
        # Restart PP so subsequent runs don't break
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{REPO}:{os.path.join(REPO, 'policy_processor', 'proto')}"
        subprocess.Popen(
            [PYTHON, os.path.join(REPO, "policy_processor", "server.py")],
            cwd=REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_for_port(50051, timeout=5)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Sprint 1 System Tests")
    print("Envoy must be running on :10000 before running these tests.")
    print("=" * 60)

    # Verify Envoy is reachable
    try:
        httpx.get("http://localhost:9901/ready", timeout=2)
    except Exception:
        print(f"\n{FAIL}  Envoy admin not reachable on :9901 — is Envoy running?")
        sys.exit(1)

    test_st1_1()
    test_st1_2()

    print("\n" + "=" * 60)
    if _failures == 0:
        print(f"{PASS}  All Sprint 1 tests passed")
        sys.exit(0)
    else:
        print(f"{FAIL}  {_failures} test(s) failed")
        sys.exit(1)
