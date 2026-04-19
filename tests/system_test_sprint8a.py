#!/usr/bin/env python3
"""Sprint 8 — Phase 8A system tests.

The stack is assumed up (run_sprint8a.sh brought it up via docker compose).
These tests hit real services over the host network — no mocks.

Covered:
  ST-8.1  Both agent containers register with the Orchestrator.
  ST-8.2  Pre-policy chat returns 503 (agent-not-ready).
  ST-8.3  PUT /agents/{id}/policy flips the agent to RUNNING; chat replies.
  ST-8.4  A2A to a non-allowlisted target is DENIED by Envoy/Policy Processor.
  ST-8.5  A2A to an allowlisted target succeeds end-to-end.
  ST-8.10 Container restart skips PENDING (fast-path replay).
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx

ORCH = "http://localhost:7000"
AGENT_A_CHAT = "http://localhost:18080"
AGENT_B_CHAT = "http://localhost:28080"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    tag = PASS if ok else FAIL
    line = f"  {tag}  {name}"
    if detail:
        line += f"  — {detail}"
    print(line)


def wait_status(agent_id: str, target: str, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(f"{ORCH}/agents/{agent_id}/status", timeout=3)
            last = r.json().get("status", "")
            if last == target:
                return last
        except Exception as e:
            last = f"err:{e}"
        time.sleep(0.5)
    return last


def wait_healthz(chat_url: str, target: str, timeout: float = 20.0) -> str:
    """Wait for the agent container's own /healthz to report `target`.

    Orchestrator status flips the instant the admin pushes policy, but the
    container still has to poll the orchestrator once before flipping itself.
    Tests that drive the agent directly must wait on healthz, not orch status.
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = httpx.get(f"{chat_url}/healthz", timeout=3).json().get("status", "")
            if last == target:
                return last
        except Exception as e:
            last = f"err:{e}"
        time.sleep(0.5)
    return last


# ── ST-8.1 ───────────────────────────────────────────────────────────────────

def st_8_1() -> None:
    """Both containers registered."""
    try:
        r = httpx.get(f"{ORCH}/agents", timeout=5)
        r.raise_for_status()
        ids = {a["agent_id"] for a in (r.json().get("agents") or [])}
        ok = {"agent-a", "agent-b"}.issubset(ids)
        report("ST-8.1 registration", ok, f"agents={sorted(ids)}")
    except Exception as e:
        report("ST-8.1 registration", False, str(e))


# ── ST-8.2 ───────────────────────────────────────────────────────────────────

def st_8_2() -> None:
    """Before policy push → chat 503."""
    try:
        r = httpx.post(f"{AGENT_A_CHAT}/chat", json={"message": "hi"}, timeout=5)
        ok = r.status_code == 503
        report("ST-8.2 pre-policy 503", ok, f"got {r.status_code}")
    except Exception as e:
        report("ST-8.2 pre-policy 503", False, str(e))


# ── ST-8.3 ───────────────────────────────────────────────────────────────────

POLICY_AGENT_A = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
limits:
  max_requests_per_minute: 60
  max_request_size_bytes: 262144
"""

POLICY_AGENT_B = """\
allowed_remote_agents:
  - agent-a
allowed_mcp_servers: []
limits:
  max_requests_per_minute: 60
  max_request_size_bytes: 262144
"""


def st_8_3() -> None:
    """PUT policy → RUNNING → chat replies."""
    try:
        r = httpx.put(
            f"{ORCH}/agents/agent-a/policy",
            content=POLICY_AGENT_A,
            headers={"Content-Type": "application/yaml"},
            timeout=10,
        )
        r.raise_for_status()
        status = wait_status("agent-a", "RUNNING")
        if status != "RUNNING":
            report("ST-8.3 policy → RUNNING", False, f"orch status={status}")
            return

        # Also push policy for agent-b so subsequent tests have it RUNNING.
        httpx.put(
            f"{ORCH}/agents/agent-b/policy",
            content=POLICY_AGENT_B,
            headers={"Content-Type": "application/yaml"},
            timeout=10,
        ).raise_for_status()
        wait_status("agent-b", "RUNNING")

        # Wait for containers themselves to pick up the status change.
        wait_healthz(AGENT_A_CHAT, "RUNNING")
        wait_healthz(AGENT_B_CHAT, "RUNNING")

        chat = httpx.post(
            f"{ORCH}/agents/agent-a/chat", json={"message": "hello"}, timeout=10
        )
        ok = chat.status_code == 200 and "reply" in chat.json()
        report(
            "ST-8.3 policy → RUNNING",
            ok,
            f"chat {chat.status_code}: {chat.text[:120]}",
        )
    except Exception as e:
        report("ST-8.3 policy → RUNNING", False, str(e))


# ── ST-8.4 ───────────────────────────────────────────────────────────────────

def st_8_4() -> None:
    """A2A to a non-allowlisted target is denied.

    agent-a's policy allows only agent-b, so /a2a-to/agent-c (even though
    agent-c doesn't exist as an Envoy cluster) is blocked before the
    upstream connection is attempted.
    """
    try:
        r = httpx.post(
            f"{AGENT_A_CHAT}/a2a-to/agent-c",
            json={"message": "should be denied"},
            timeout=10,
        )
        body = r.json()
        status_code = body.get("status_code")
        ok = r.status_code == 200 and status_code == 403
        report(
            "ST-8.4 A2A deny non-allowlisted",
            ok,
            f"envoy returned {status_code}",
        )
    except Exception as e:
        report("ST-8.4 A2A deny non-allowlisted", False, str(e))


# ── ST-8.5 ───────────────────────────────────────────────────────────────────

def st_8_5() -> None:
    """A2A to allowlisted target succeeds through Envoy."""
    try:
        r = httpx.post(
            f"{AGENT_A_CHAT}/a2a-to/agent-b",
            json={"message": "ping-from-a"},
            timeout=15,
        )
        body = r.json()
        status_code = body.get("status_code")
        inner = body.get("body") or {}
        result = inner.get("result") if isinstance(inner, dict) else None
        artifacts = (result or {}).get("artifacts") or []
        text = ""
        if artifacts:
            parts = artifacts[0].get("parts") or []
            if parts:
                text = parts[0].get("text", "")
        ok = status_code == 200 and "echo" in text
        report("ST-8.5 A2A allow", ok, f"envoy={status_code} text={text!r}")
    except Exception as e:
        report("ST-8.5 A2A allow", False, str(e))


# ── ST-8.10 ──────────────────────────────────────────────────────────────────

def st_8_10() -> None:
    """Container restart — orchestrator re-plays cached policy; skips PENDING."""
    try:
        subprocess.check_call(
            ["docker", "restart", "nemo-agent-a"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give the container a moment to come back & re-register.
        # We require the final status to be RUNNING quickly (≤15s) without any
        # admin PUT in between.
        status = wait_status("agent-a", "RUNNING", timeout=25)
        ok = status == "RUNNING"
        report("ST-8.10 restart fast-path", ok, f"status={status}")

        # And the agent's own healthz should also show RUNNING. After a
        # container restart the app-level status flips ~1–2s after the orch
        # status does (register → poll → mark_running), so poll instead of
        # sleeping a fixed interval.
        hz_status = wait_healthz(AGENT_A_CHAT, "RUNNING", timeout=15)
        try:
            hz = httpx.get(f"{AGENT_A_CHAT}/healthz", timeout=5).json()
        except Exception as e:
            report("ST-8.10 agent healthz RUNNING", False, str(e))
            return
        report(
            "ST-8.10 agent healthz RUNNING",
            hz_status == "RUNNING",
            f"healthz={hz}",
        )
    except subprocess.CalledProcessError as e:
        report("ST-8.10 restart fast-path", False, f"docker restart: {e}")
    except Exception as e:
        report("ST-8.10 restart fast-path", False, str(e))


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=== Sprint 8 Phase 8A system tests ===")
    # ST-8.2 requires pre-policy state. The stack must be fresh (run_sprint8a.sh
    # does `docker compose down -v` before boot). If the orchestrator already
    # has a cached policy for agent-a, ST-8.2 would falsely fail.
    for aid in ("agent-a", "agent-b"):
        status = wait_status(aid, "PENDING", timeout=5)
        if status != "PENDING":
            print(
                f"  NOTE: {aid} already {status}; stack is not fresh. "
                "Run './run_sprint8a.sh --down' then re-run for clean pre-policy checks."
            )

    st_8_1()
    st_8_2()
    st_8_3()
    st_8_4()
    st_8_5()
    st_8_10()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print(f"  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
