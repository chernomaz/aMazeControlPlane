#!/usr/bin/env python3
"""Sprint 8 — Phase 8B system tests.

The stack (orchestrator + policy-processor + envoy + agent-a + agent-b +
mcp-5-tools) is assumed up via run_sprint8b.sh. These tests hit real services
over the host network — no mocks — and ST-8.7a makes a *live* Tavily call.

Covered:
  ST-8.11 MCP-server NEMO container registers exactly once with the
          Orchestrator on startup and appears in GET /mcp.
  ST-8.7a MCP tools/call — allowed server + allowed tool → 200 with a
          non-empty Tavily result (real upstream call).
  ST-8.7b MCP tools/call — server not in allowlist → 403
          reason=mcp-server-not-allowed.
  ST-8.7c MCP tools/call — server allowed, tool not allowed → 403
          reason=tool-not-allowed.

Run from the compose host (the docker CLI is used for container-liveness
checks in ST-8.11).
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx

ORCH = "http://localhost:7000"
AGENT_A_CHAT = "http://localhost:18080"

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


# ── ST-8.11 ──────────────────────────────────────────────────────────────────

def container_running(name: str) -> bool:
    """True iff the named container is in docker's 'running' state."""
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return False
    return out.decode().strip() == "running"


def fetch_entry(mcp_id: str) -> dict | None:
    r = httpx.get(f"{ORCH}/mcp", timeout=3)
    r.raise_for_status()
    for m in r.json().get("mcp_servers") or []:
        if m.get("mcp_id") == mcp_id:
            return m
    return None


def st_8_11() -> None:
    """MCP container registers exactly once on startup.

    Three claims are asserted:
      1. The entry is present in GET /mcp with the expected host/port.
      2. The docker container is still 'running' after registration — rules
         out the register-then-crash failure mode.
      3. registered_at is stable across a 3-second window — proves the
         container is NOT re-POSTing in a loop (the "once" in the ST name).
    """
    deadline = time.time() + 15
    entry: dict | None = None
    last_err: str = ""
    while time.time() < deadline:
        try:
            entry = fetch_entry("mcp-5-tools")
            if entry is not None:
                break
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5)

    if entry is None:
        report("ST-8.11 MCP registers once", False, f"mcp-5-tools not found; {last_err}")
        return

    t0 = entry.get("registered_at")
    time.sleep(3)
    try:
        entry2 = fetch_entry("mcp-5-tools")
    except Exception as e:
        report("ST-8.11 MCP registers once", False, f"re-read failed: {e}")
        return
    if entry2 is None:
        report("ST-8.11 MCP registers once", False, "mcp-5-tools disappeared on re-read")
        return

    running = container_running("nemo-mcp-5-tools")
    once = entry2.get("registered_at") == t0

    ok = (
        entry.get("host") == "mcp-5-tools"
        and entry.get("port") == 8000
        and running
        and once
    )
    report(
        "ST-8.11 MCP registers once",
        ok,
        f"entry={entry} running={running} once={once}",
    )


# ── ST-8.7a/b/c setup ────────────────────────────────────────────────────────

# Three variants of agent-a's policy. ST-8.3 already left agent-a RUNNING; each
# test here PUTs a different policy, which updates the Policy Processor's
# in-memory store immediately. No container restart is needed — the processor
# looks up policy on every request.

POLICY_A_ALLOW_MCP = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers:
  - mcp-5-tools
allowed_tools:
  mcp-5-tools:
    - web_search
limits:
  max_request_size_bytes: 262144
"""

POLICY_A_NO_MCP = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
limits:
  max_request_size_bytes: 262144
"""

POLICY_A_SERVER_ONLY = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers:
  - mcp-5-tools
allowed_tools:
  mcp-5-tools: []
limits:
  max_request_size_bytes: 262144
"""


def put_policy(agent_id: str, yaml_text: str) -> None:
    r = httpx.put(
        f"{ORCH}/agents/{agent_id}/policy",
        content=yaml_text,
        headers={"Content-Type": "application/yaml"},
        timeout=10,
    )
    r.raise_for_status()


def call_mcp(chat_url: str, server: str, tool: str, args: dict) -> dict:
    """POST to the agent's /mcp-call helper.

    The endpoint always returns HTTP 200 with a JSON body describing what
    happened (ok=True on success, ok=False with an error string on any
    failure — policy deny, transport error, MCP error, etc.).
    """
    r = httpx.post(
        f"{chat_url}/mcp-call/{server}/{tool}",
        json={"arguments": args},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


# ── ST-8.7a ──────────────────────────────────────────────────────────────────

def st_8_7a() -> None:
    """Allow + allow — real Tavily call returns non-empty text."""
    put_policy("agent-a", POLICY_A_ALLOW_MCP)
    try:
        resp = call_mcp(
            AGENT_A_CHAT,
            "mcp-5-tools",
            "web_search",
            {"query": "aMaze control plane agent framework"},
        )
    except Exception as e:
        report("ST-8.7a allow + real tavily", False, f"call failed: {e}")
        return

    if not resp.get("ok"):
        # Distinguish live-Tavily failures from enforcement failures so the
        # test report is actionable (a Tavily outage is not a policy bug).
        err = (resp.get("error") or "").lower()
        detail = f"ok=false err={err[:160]}"
        if "tavily" in err or "timeout" in err or "upstream" in err:
            detail = f"UPSTREAM FAILURE (Tavily): {err[:160]}"
        report("ST-8.7a allow + real tavily", False, detail)
        return

    text = ""
    for c in resp.get("content") or []:
        if c.get("text"):
            text = c["text"]
            break
    ok = len(text) > 50  # real tavily results are substantial
    report("ST-8.7a allow + real tavily", ok, f"len(text)={len(text)}")


# ── ST-8.7b ──────────────────────────────────────────────────────────────────

def st_8_7b() -> None:
    """Server not in allowlist — tools/call denied with mcp-server-not-allowed."""
    put_policy("agent-a", POLICY_A_NO_MCP)
    try:
        resp = call_mcp(AGENT_A_CHAT, "mcp-5-tools", "web_search", {"query": "x"})
    except Exception as e:
        report("ST-8.7b server not allowed", False, f"call failed: {e}")
        return
    ok = (
        not resp.get("ok")
        and resp.get("status_code") == 403
        and resp.get("reason") == "mcp-server-not-allowed"
    )
    report(
        "ST-8.7b server not allowed",
        ok,
        f"status={resp.get('status_code')} reason={resp.get('reason')}",
    )


# ── ST-8.7c ──────────────────────────────────────────────────────────────────

def st_8_7c() -> None:
    """Server allowed, tool not — tools/call denied with tool-not-allowed."""
    put_policy("agent-a", POLICY_A_SERVER_ONLY)
    try:
        resp = call_mcp(AGENT_A_CHAT, "mcp-5-tools", "web_search", {"query": "x"})
    except Exception as e:
        report("ST-8.7c tool not allowed", False, f"call failed: {e}")
        return
    ok = (
        not resp.get("ok")
        and resp.get("status_code") == 403
        and resp.get("reason") == "tool-not-allowed"
    )
    report(
        "ST-8.7c tool not allowed",
        ok,
        f"status={resp.get('status_code')} reason={resp.get('reason')}",
    )


# ── main ─────────────────────────────────────────────────────────────────────

POLICY_A_RESTORE = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
limits:
  max_requests_per_minute: 60
  max_request_size_bytes: 262144
"""


def main() -> int:
    print("=== Sprint 8 Phase 8B system tests ===")
    st_8_11()
    st_8_7a()
    st_8_7b()
    st_8_7c()

    # Tests above mutated agent-a's policy. Restore the canonical 8A policy so
    # a follow-up run of system_test_sprint8a.py doesn't see leaked state.
    try:
        put_policy("agent-a", POLICY_A_RESTORE)
    except Exception as e:
        print(f"  NOTE: policy restore failed: {e}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print(f"  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
