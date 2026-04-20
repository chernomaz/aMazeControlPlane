#!/usr/bin/env python3
"""Sprint 8 Phase 8B Slice 4 — A2A bearer-token identity.

The stack (orchestrator + policy-processor + envoy + agent-a + agent-b +
mcp-5-tools + litellm) is assumed up via run_sprint8b.sh.

Covered:
  ST-8.8a  Bearer-resolved identity → A2A allowed end-to-end
           (agent-a's A2A client injects `Authorization: Bearer
           <token>` returned by the orchestrator at register; ext_proc
           resolves the token to `agent-a` — no x-agent-id header on the
           wire — and runs the normal A2A allowlist check).
  ST-8.8b  Invalid bearer → Envoy 403 reason=invalid-bearer.
  ST-8.8c  Missing bearer on A2A → Envoy 403 reason=missing-bearer.

Depends on ST-8.3 having already pushed policy for agent-a (allowed_remote_agents
includes agent-b). run_sprint8b.sh runs 8A first, so that's already true.

The test uses the agent's /a2a-to/{target} helper which accepts an
`auth_override` field in the request body — set to "missing" to exercise
ST-8.8c and to a custom "Bearer garbage" string for ST-8.8b. This avoids
introducing a second helper endpoint just for testing.
"""

from __future__ import annotations

import sys

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


def a2a_to(target: str, auth_override: str | None = None) -> dict:
    """POST /a2a-to/{target} with optional auth-header override.

    Returns the JSON body (always 200 from the helper; the real Envoy
    status lives in body["status_code"]).
    """
    body = {"message": "bearer-test"}
    if auth_override is not None:
        body["auth_override"] = auth_override
    r = httpx.post(f"{AGENT_A_CHAT}/a2a-to/{target}", json=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ── ST-8.8a ──────────────────────────────────────────────────────────────────

def st_8_8a() -> None:
    """Bearer-resolved identity → A2A to allowlisted target succeeds."""
    try:
        resp = a2a_to("agent-b")
    except Exception as e:
        report("ST-8.8a bearer-resolved allow", False, f"call failed: {e}")
        return
    status_code = resp.get("status_code")
    inner = resp.get("body") or {}
    artifacts = ((inner.get("result") or {}).get("artifacts") or []) if isinstance(inner, dict) else []
    text = ""
    if artifacts:
        parts = artifacts[0].get("parts") or []
        if parts:
            text = parts[0].get("text", "")
    ok = status_code == 200 and "echo" in text
    report(
        "ST-8.8a bearer-resolved allow",
        ok,
        f"envoy={status_code} text={text!r}",
    )


# ── ST-8.8b ──────────────────────────────────────────────────────────────────

def st_8_8b() -> None:
    """Invalid bearer → 403 invalid-bearer."""
    try:
        resp = a2a_to("agent-b", auth_override="Bearer not-a-real-token-deadbeef")
    except Exception as e:
        report("ST-8.8b invalid-bearer", False, f"call failed: {e}")
        return
    status_code = resp.get("status_code")
    inner = resp.get("body")
    reason = None
    if isinstance(inner, dict):
        reason = inner.get("reason")
    ok = status_code == 403 and reason == "invalid-bearer"
    report(
        "ST-8.8b invalid-bearer",
        ok,
        f"envoy={status_code} reason={reason}",
    )


# ── ST-8.8c ──────────────────────────────────────────────────────────────────

def st_8_8c() -> None:
    """Missing bearer on A2A → 403 missing-bearer."""
    try:
        resp = a2a_to("agent-b", auth_override="missing")
    except Exception as e:
        report("ST-8.8c missing-bearer", False, f"call failed: {e}")
        return
    status_code = resp.get("status_code")
    inner = resp.get("body")
    reason = None
    if isinstance(inner, dict):
        reason = inner.get("reason")
    ok = status_code == 403 and reason == "missing-bearer"
    report(
        "ST-8.8c missing-bearer",
        ok,
        f"envoy={status_code} reason={reason}",
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=== Sprint 8 Phase 8B Slice 4 — A2A bearer token ===")
    st_8_8a()
    st_8_8b()
    st_8_8c()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print(f"  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
