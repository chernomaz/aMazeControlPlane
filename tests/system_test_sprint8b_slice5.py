#!/usr/bin/env python3
"""Sprint 8 Phase 8B Slice 5 — cross-org A2A via a2a_proxy sidecar.

The stack (orchestrator + policy-processor + envoy + agent-a + agent-b +
mcp-5-tools + litellm + a2a-proxy + partner-agent) is assumed up via
run_sprint8b.sh.

Covered:
  ST-8.9a  Cross-org A2A allow-path — agent-a's policy lists the partner
           FQDN; request flows agent → Envoy (sees plaintext JSON-RPC) →
           a2a_proxy → TLS → partner-agent; 200 with partner's echo reply.
  ST-8.9b  Unknown JSON-RPC method denied — same destination with
           method=admin/delete → ext_proc 403 reason=unknown-method.
  ST-8.9c  Unlisted partner denied — allowed_remote_agents omits the
           partner FQDN; ext_proc 403 reason=not-allowed; partner never
           contacted.

Each test PUTs a specific policy for agent-a then calls the agent's
/cross-org-a2a helper. The canonical 8A policy is restored at the end so
a follow-up 8A run doesn't see leaked state.
"""

from __future__ import annotations

import sys

import httpx

ORCH = "http://localhost:7000"
AGENT_A_CHAT = "http://localhost:18080"
PARTNER_URL = "https://partner-agent.example.com/a2a"

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


def put_policy(agent_id: str, yaml_text: str) -> None:
    r = httpx.put(
        f"{ORCH}/agents/{agent_id}/policy",
        content=yaml_text,
        headers={"Content-Type": "application/yaml"},
        timeout=10,
    )
    r.raise_for_status()


def cross_org(target: str, message: str, method: str | None = None) -> dict:
    body = {"target": target, "message": message}
    if method is not None:
        body["method"] = method
    r = httpx.post(f"{AGENT_A_CHAT}/cross-org-a2a", json=body, timeout=25)
    r.raise_for_status()
    return r.json()


# ── Policy variants ──────────────────────────────────────────────────────────

POLICY_ALLOW_PARTNER = """\
allowed_remote_agents:
  - agent-b
  - partner-agent.example.com
allowed_mcp_servers: []
limits:
  max_request_size_bytes: 262144
"""

POLICY_BLOCK_PARTNER = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
limits:
  max_request_size_bytes: 262144
"""

POLICY_RESTORE_8A = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
limits:
  max_requests_per_minute: 60
  max_request_size_bytes: 262144
"""


# ── ST-8.9a ──────────────────────────────────────────────────────────────────

def st_8_9a() -> None:
    put_policy("agent-a", POLICY_ALLOW_PARTNER)
    try:
        resp = cross_org(PARTNER_URL, "hello-partner")
    except Exception as e:
        report("ST-8.9a cross-org allow", False, f"call failed: {e}")
        return
    status_code = resp.get("status_code")
    inner = resp.get("body") or {}
    artifacts = ((inner.get("result") or {}).get("artifacts") or []) if isinstance(inner, dict) else []
    text = ""
    if artifacts:
        parts = artifacts[0].get("parts") or []
        if parts:
            text = parts[0].get("text", "")
    ok = status_code == 200 and "cross-org echo" in text
    report(
        "ST-8.9a cross-org allow",
        ok,
        f"envoy={status_code} text={text!r}",
    )


# ── ST-8.9b ──────────────────────────────────────────────────────────────────

def st_8_9b() -> None:
    put_policy("agent-a", POLICY_ALLOW_PARTNER)
    try:
        resp = cross_org(PARTNER_URL, "naughty", method="admin/delete")
    except Exception as e:
        report("ST-8.9b unknown-method denied", False, f"call failed: {e}")
        return
    status_code = resp.get("status_code")
    inner = resp.get("body")
    reason = inner.get("reason") if isinstance(inner, dict) else None
    ok = status_code == 403 and reason == "unknown-method"
    report(
        "ST-8.9b unknown-method denied",
        ok,
        f"envoy={status_code} reason={reason}",
    )


# ── ST-8.9c ──────────────────────────────────────────────────────────────────

def st_8_9c() -> None:
    put_policy("agent-a", POLICY_BLOCK_PARTNER)
    try:
        resp = cross_org(PARTNER_URL, "should-fail")
    except Exception as e:
        report("ST-8.9c partner not allowed", False, f"call failed: {e}")
        return
    status_code = resp.get("status_code")
    inner = resp.get("body")
    reason = inner.get("reason") if isinstance(inner, dict) else None
    ok = status_code == 403 and reason == "not-allowed"
    report(
        "ST-8.9c partner not allowed",
        ok,
        f"envoy={status_code} reason={reason}",
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=== Sprint 8 Phase 8B Slice 5 — cross-org A2A ===")
    st_8_9a()
    st_8_9b()
    st_8_9c()

    try:
        put_policy("agent-a", POLICY_RESTORE_8A)
    except Exception as e:
        print(f"  NOTE: policy restore failed: {e}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print(f"  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
