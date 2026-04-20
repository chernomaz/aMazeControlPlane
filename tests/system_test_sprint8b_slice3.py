#!/usr/bin/env python3
"""Sprint 8 Phase 8B Slice 3 — LiteLLM sidecar + zero-change LLM enforcement.

The stack (orchestrator + policy-processor + envoy + agent-a + agent-b +
mcp-5-tools + litellm) is assumed up via run_sprint8b.sh.

Covered:
  ST-8.6a Zero-change openai.OpenAI() in agent code → routes via Envoy →
          LiteLLM → api.openai.com; /stats/agents/{id} shows tokens > 0.
  ST-8.6b Provider not in policy.allowed_llms → Envoy 403
          reason=llm-not-allowed (no upstream call).
  ST-8.6c Token cap — second call after a >cap first call → Envoy 429
          reason=token-limit-exceeded.

Notes on test isolation:
  - Each variant PUTs a specific policy for agent-a and waits for the
    container's healthz to report RUNNING before making the call.
  - Agent-a's canonical 8A policy is restored at the end so a follow-up run
    of system_test_sprint8a.py doesn't see leaked state.
  - ST-8.6a's assertion on tokens reads policy-processor stats, which is
    the only authoritative source for what ext_proc extracted.
"""

from __future__ import annotations

import sys
import time

import httpx

ORCH = "http://localhost:7000"
PP_STATS = "http://localhost:8081"
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


def put_policy(agent_id: str, yaml_text: str) -> None:
    r = httpx.put(
        f"{ORCH}/agents/{agent_id}/policy",
        content=yaml_text,
        headers={"Content-Type": "application/yaml"},
        timeout=10,
    )
    r.raise_for_status()


def get_stats(agent_id: str) -> dict:
    r = httpx.get(f"{PP_STATS}/stats/agents/{agent_id}", timeout=3)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def call_llm(prompt: str) -> dict:
    """POST to the agent's /llm-call helper. Always returns 200 JSON."""
    r = httpx.post(
        f"{AGENT_A_CHAT}/llm-call",
        json={"model": "gpt-4o-mini", "prompt": prompt},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()


# ── Policy variants ──────────────────────────────────────────────────────────

POLICY_ALLOW_LLM = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
allowed_llms:
  - litellm
limits:
  max_request_size_bytes: 262144
"""

POLICY_NO_LLM = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
allowed_llms: []
limits:
  max_request_size_bytes: 262144
"""

# rate_window_seconds=30 so ST-8.6c's cap remains in effect for the second
# call without the whole test waiting a full minute.
POLICY_TINY_BUDGET = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
allowed_llms:
  - litellm
limits:
  max_request_size_bytes: 262144
  max_tokens_per_minute: 10
  rate_window_seconds: 30
"""

POLICY_RESTORE_8A = """\
allowed_remote_agents:
  - agent-b
allowed_mcp_servers: []
limits:
  max_requests_per_minute: 60
  max_request_size_bytes: 262144
"""


# ── ST-8.6a ──────────────────────────────────────────────────────────────────

def st_8_6a() -> None:
    put_policy("agent-a", POLICY_ALLOW_LLM)
    before = get_stats("agent-a")
    tokens_before = (before.get("tokens_per_5min") or 0) if before else 0

    try:
        resp = call_llm("Say the word 'hello' and nothing else.")
    except Exception as e:
        report("ST-8.6a zero-change openai call", False, f"call failed: {e}")
        return

    if not resp.get("ok"):
        report(
            "ST-8.6a zero-change openai call",
            False,
            f"ok=false status={resp.get('status_code')} reason={resp.get('reason')} err={(resp.get('error') or '')[:140]}",
        )
        return

    # Give stats a moment to register the response-body phase.
    deadline = time.time() + 5
    tokens_after = tokens_before
    while time.time() < deadline:
        after = get_stats("agent-a")
        tokens_after = (after.get("tokens_per_5min") or 0) if after else 0
        if tokens_after > tokens_before:
            break
        time.sleep(0.3)

    text = resp.get("text") or ""
    ok = bool(text.strip()) and tokens_after > tokens_before
    report(
        "ST-8.6a zero-change openai call",
        ok,
        f"text={text!r} tokens {tokens_before}->{tokens_after}",
    )


# ── ST-8.6b ──────────────────────────────────────────────────────────────────

def st_8_6b() -> None:
    put_policy("agent-a", POLICY_NO_LLM)
    try:
        resp = call_llm("this should be denied")
    except Exception as e:
        report("ST-8.6b llm-not-allowed", False, f"call failed: {e}")
        return
    # Post code-review — llm-not-allowed is "forbidden, don't retry" → 403.
    # Only token-limit-exceeded keeps 429 (rate-limited).
    ok = (
        not resp.get("ok")
        and resp.get("status_code") == 403
        and resp.get("reason") == "llm-not-allowed"
    )
    report(
        "ST-8.6b llm-not-allowed",
        ok,
        f"status={resp.get('status_code')} reason={resp.get('reason')}",
    )


# ── ST-8.6c ──────────────────────────────────────────────────────────────────

def st_8_6c() -> None:
    # Fresh window: use a brand-new agent id so prior ST-8.6a tokens don't
    # leak into the cap. agent-a's x-amaze-agent-id is injected by the SDK
    # patch from AMAZE_AGENT_ID env, so it's fixed to "agent-a" inside the
    # container. Instead we reset by relying on the new budget policy — the
    # cap applies per sliding window, and POLICY_TINY_BUDGET resets the
    # cap, but the tracker window itself retains prior recorded tokens.
    #
    # To get a clean run, we wait out the prior 30s window after ST-8.6a.
    # This is cheaper than reshaping the token tracker; if flaky we can
    # add a reset endpoint later.
    time.sleep(31)
    put_policy("agent-a", POLICY_TINY_BUDGET)

    # First call: small prompt but large enough to exceed 10 tokens total.
    try:
        first = call_llm("Name three colors in one short sentence.")
    except Exception as e:
        report("ST-8.6c token cap triggers", False, f"first call failed: {e}")
        return
    if not first.get("ok"):
        report(
            "ST-8.6c token cap triggers",
            False,
            f"first call not ok: status={first.get('status_code')} reason={first.get('reason')} err={(first.get('error') or '')[:120]}",
        )
        return
    first_tokens = first.get("tokens") or 0
    if first_tokens <= 10:
        report(
            "ST-8.6c token cap triggers",
            False,
            f"first call didn't exceed cap ({first_tokens} tokens)",
        )
        return

    # Second call: budget should be exhausted -> 429 llm token-limit-exceeded.
    # Ext_proc may return 429 (DecideLLM path) with the deny JSON body.
    try:
        second = call_llm("Say hi")
    except Exception as e:
        report("ST-8.6c token cap triggers", False, f"second call failed: {e}")
        return
    ok = (
        not second.get("ok")
        and second.get("status_code") == 429
        and second.get("reason") == "token-limit-exceeded"
    )
    report(
        "ST-8.6c token cap triggers",
        ok,
        f"first={first_tokens}tok second: status={second.get('status_code')} reason={second.get('reason')}",
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=== Sprint 8 Phase 8B Slice 3 — LiteLLM enforcement ===")
    st_8_6a()
    st_8_6b()
    st_8_6c()

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
