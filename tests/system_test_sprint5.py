"""
system_test_sprint5.py — Sprint 5: Go Policy Processor regression suite.

Assumes the stack is already running (run_sprint5.sh).
Covers ST-5.1 (all prior behaviour), ST-5.2 (concurrency), ST-5.3 (SIGHUP).

ST-5.1 re-runs key assertions from Sprints 1–4 against the Go processor:
  ST-1.2 fail-closed, ST-2.2 not-allowed, ST-2.3 unknown-caller,
  ST-3.2 tool-not-allowed, ST-3.3 mcp-server-not-allowed,
  ST-4.2 request-too-large, ST-4.1 rate-limit-exceeded, ST-4.3 call-limit-exceeded
"""
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENVOY = "http://localhost:10000"
ADMIN = "http://localhost:9901"
PP_PORT = 50051

_failures = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    mark = "\033[32mPASS\033[0m" if condition else "\033[31mFAIL\033[0m"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        _failures += 1


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def is_denied(resp: httpx.Response, reason: str) -> bool:
    if resp.status_code != 403:
        return False
    try:
        return resp.json().get("reason") == reason
    except Exception:
        return False


def a2a(caller: str, target: str, payload: str = "hello") -> httpx.Response:
    return httpx.post(
        f"{ENVOY}/",
        headers={"x-agent-id": caller, "host": target, "content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/send",
              "params": {"message": payload}},
        timeout=5,
    )


def mcp_call(caller: str, server: str, tool: str) -> httpx.Response:
    return httpx.post(
        f"{ENVOY}/",
        headers={"x-agent-id": caller, "host": server,
                 "content-type": "application/json", "accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": {}}},
        timeout=5,
    )


# ── Preflight ─────────────────────────────────────────────────────────────────

print("\n=== Sprint 5 — Go Policy Processor ===\n")
print("Waiting for services...")
check("Go processor :50051 reachable", wait_for_port(PP_PORT, 10))
check("Envoy :9901/ready", wait_for_port(9901, 10) and
      httpx.get(f"{ADMIN}/ready", timeout=2).status_code == 200)


# ── ST-5.1a  Fail-closed (ST-1.2 equivalent) ─────────────────────────────────

print("\nST-5.1a — Fail-closed")

# find Go processor PID via lsof
result = subprocess.run(
    ["lsof", "-ti", f":{PP_PORT}"], capture_output=True, text=True
)
go_pid = int(result.stdout.strip().split("\n")[0]) if result.stdout.strip() else None

if go_pid:
    os.kill(go_pid, signal.SIGTERM)
    time.sleep(1.5)
    resp = httpx.post(
        f"{ENVOY}/",
        headers={"x-agent-id": "agent-a", "host": "agent-b",
                 "content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tasks/send", "params": {}},
        timeout=5,
    )
    check("ST-1.2 processor down → Envoy denies (non-200)", resp.status_code != 200,
          f"status={resp.status_code}")

    # restart Go processor
    go_bin = os.path.join(REPO, "go_processor", "go-policy-processor")
    policy_path = os.path.join(REPO, "policy_processor", "policies", "agents.yaml")
    proc = subprocess.Popen(
        [go_bin],
        env={**os.environ, "POLICY_PATH": policy_path},
    )
    check("Go processor restarted", wait_for_port(PP_PORT, 8), f"PID={proc.pid}")
    go_pid = proc.pid
else:
    check("ST-1.2 skipped (could not find Go processor PID)", True, "skipped")


# ── ST-5.1b  A2A enforcement (ST-2.2, ST-2.3) ────────────────────────────────

print("\nST-5.1b — A2A enforcement")
resp = a2a("agent-a", "agent-c")
check("ST-2.2 agent-a → agent-c denied not-allowed",
      is_denied(resp, "not-allowed"), f"status={resp.status_code}")

resp = a2a("unknown-agent", "agent-b")
check("ST-2.3 unknown-agent denied unknown-caller",
      is_denied(resp, "unknown-caller"), f"status={resp.status_code}")


# ── ST-5.1c  MCP enforcement (ST-3.2, ST-3.3) ────────────────────────────────

print("\nST-5.1c — MCP enforcement")
resp = mcp_call("agent-a-mcp", "mcp-server", "dangerous_tool")
check("ST-3.2 dangerous_tool denied tool-not-allowed",
      is_denied(resp, "tool-not-allowed"), f"status={resp.status_code}")

resp = mcp_call("agent-a-mcp", "unlisted-mcp", "echo")
check("ST-3.3 unlisted-mcp denied mcp-server-not-allowed",
      is_denied(resp, "mcp-server-not-allowed"), f"status={resp.status_code}")


# ── ST-5.1d  Limits (ST-4.1 rate, ST-4.2 size, ST-4.3 call count) ────────────

print("\nST-5.1d — Limits")

# ST-4.2 oversized A2A body
big = "x" * 300
resp = httpx.post(
    f"{ENVOY}/",
    headers={"x-agent-id": "agent-a", "host": "agent-b",
             "content-type": "application/json"},
    json={"jsonrpc": "2.0", "id": 1, "method": "tasks/send",
          "params": {"data": big}},
    timeout=5,
)
check("ST-4.2 oversized body denied request-too-large",
      is_denied(resp, "request-too-large"), f"status={resp.status_code}")

# ST-4.1 rate limit (agent-a-mcp: max 2 req / 3 s window)
# drain any leftover window from previous tests
time.sleep(3)
for i in range(2):
    r = mcp_call("agent-a-mcp", "mcp-server", "echo")
    check(f"ST-4.1 request {i+1}/2 passes (not rate-limited)",
          not is_denied(r, "rate-limit-exceeded"), f"status={r.status_code}")
r = mcp_call("agent-a-mcp", "mcp-server", "echo")
check("ST-4.1 request 3/2 denied rate-limit-exceeded",
      is_denied(r, "rate-limit-exceeded"), f"status={r.status_code}")

# ST-4.3 call count (agent-a-mcp-counter: max 2 echo calls, no rate limit on this agent)
for i in range(2):
    r = mcp_call("agent-a-mcp-counter", "mcp-server", "echo")
    check(f"ST-4.3 call {i+1}/2 passes",
          not is_denied(r, "call-limit-exceeded"), f"status={r.status_code}")
r = mcp_call("agent-a-mcp-counter", "mcp-server", "echo")
check("ST-4.3 call 3/2 denied call-limit-exceeded",
      is_denied(r, "call-limit-exceeded"), f"status={r.status_code}")


# ── ST-5.2  Concurrency — 20 simultaneous requests ───────────────────────────

print("\nST-5.2 — Concurrency (20 simultaneous requests)")

results: list[httpx.Response] = [None] * 20  # type: ignore
errors: list[Exception] = []

def fire(idx: int) -> None:
    try:
        results[idx] = a2a("unknown-agent", "agent-b")
    except Exception as exc:
        errors.append(exc)

threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("ST-5.2 no exceptions in 20 concurrent requests",
      len(errors) == 0, f"errors={errors}")
def _is_json(r):
    try:
        r.json()
        return True
    except Exception:
        return False

check("ST-5.2 all 20 received parseable JSON response",
      all(r is not None and _is_json(r) for r in results),
      f"non-json={sum(1 for r in results if r is None or not _is_json(r))}")
check("ST-5.2 all 20 denied unknown-caller",
      all(is_denied(r, "unknown-caller") for r in results if r is not None),
      f"wrong={sum(1 for r in results if r and not is_denied(r, 'unknown-caller'))}")


# ── ST-5.3  SIGHUP hot-reload ─────────────────────────────────────────────────

print("\nST-5.3 — SIGHUP hot-reload")

policy_path = os.path.join(REPO, "policy_processor", "policies", "agents.yaml")
with open(policy_path) as f:
    original_yaml = f.read()

new_agent_yaml = original_yaml + """
  sighup-test-agent:
    allowed_remote_agents:
      - agent-b
"""

try:
    with open(policy_path, "w") as f:
        f.write(new_agent_yaml)

    # find current Go processor PID
    result = subprocess.run(
        ["lsof", "-ti", f":{PP_PORT}"], capture_output=True, text=True
    )
    pid_str = result.stdout.strip().split("\n")[0] if result.stdout.strip() else ""
    if pid_str:
        os.kill(int(pid_str), signal.SIGHUP)
        time.sleep(0.5)
        resp = a2a("sighup-test-agent", "agent-b")
        check("ST-5.3 new agent allowed after SIGHUP",
              not is_denied(resp, "unknown-caller"),
              f"status={resp.status_code} body={resp.text[:80]}")
    else:
        check("ST-5.3 skipped (could not find PID)", True, "skipped")
finally:
    with open(policy_path, "w") as f:
        f.write(original_yaml)


# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*42}")
if _failures == 0:
    print("\033[32mAll tests passed.\033[0m")
else:
    print(f"\033[31m{_failures} test(s) failed.\033[0m")
sys.exit(0 if _failures == 0 else 1)
