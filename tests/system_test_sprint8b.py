#!/usr/bin/env python3
"""Sprint 8 — Phase 8B system tests.

The stack (orchestrator + policy-processor + envoy + agent-a + agent-b +
mcp-echo) is assumed up via run_sprint8b.sh. These tests hit real services
over the host network — no mocks.

Phase 8B ships in slices (see SPRINTS.md §Sprint 8). This file grows one
test per slice; Slice 1 covers ST-8.11 only.

Covered:
  ST-8.11 MCP-server NEMO container registers exactly once with the
          Orchestrator on startup and appears in GET /mcp.
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx

ORCH = "http://localhost:7000"

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
            entry = fetch_entry("mcp-echo")
            if entry is not None:
                break
        except Exception as e:
            last_err = str(e)
        time.sleep(0.5)

    if entry is None:
        report("ST-8.11 MCP registers once", False, f"mcp-echo not found; {last_err}")
        return

    t0 = entry.get("registered_at")
    time.sleep(3)
    try:
        entry2 = fetch_entry("mcp-echo")
    except Exception as e:
        report("ST-8.11 MCP registers once", False, f"re-read failed: {e}")
        return
    if entry2 is None:
        report("ST-8.11 MCP registers once", False, "mcp-echo disappeared on re-read")
        return

    running = container_running("nemo-mcp-echo")
    once = entry2.get("registered_at") == t0

    ok = (
        entry.get("host") == "mcp-echo"
        and entry.get("port") == 8000
        and running
        and once
    )
    report(
        "ST-8.11 MCP registers once",
        ok,
        f"entry={entry} running={running} once={once}",
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=== Sprint 8 Phase 8B system tests ===")
    st_8_11()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print(f"  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
