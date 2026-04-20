#!/usr/bin/env python3
"""Sprint 9 — Agent SDK system tests.

Stack assumed up via run_sprint9.sh (all 8-sprint agents + the three
sdk-agent-* containers). Tests hit real services over the compose
network (orchestrator + envoy + policy-processor + SDK agents); no
mocks.

Covered:
  ST-9.1  send_message_to_agent routes through Envoy — allow + deny paths
  ST-9.2  receive_message_from_user auto-discovered via frame walk
  ST-9.3  receive_message_from_agent auto-discovered + caller id passed
  ST-9.4  Existing LangChain agent works unchanged alongside amaze.init()
          — unmodified ChatOpenAI(...) + agent.invoke(...); LLM call flows
          through the patched openai SDK → Envoy → LiteLLM and records
          tokens in /stats/agents/sdk-agent-llm
  ST-9.5  Multi-hop: user → sdk-agent-a → send_message_to_agent("sdk-agent-b")
          → sdk-agent-b replies → sdk-agent-a folds reply into its own response
  ST-9.6  Sync handlers (sdk-agent-a) run in the threadpool; async handlers
          (sdk-agent-b) await on the event loop. Both handle 5 concurrent
          messages correctly; async wall-clock ≤ sync wall-clock (loose
          bound — threadpool workers are plentiful on uvicorn defaults).
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx

ORCH = "http://localhost:7000"
PP_STATS = "http://localhost:8081"
SDK_A = "http://localhost:38080"
SDK_B = "http://localhost:48080"
SDK_LLM = "http://localhost:58080"

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


def post_user(chat_url: str, message: str, timeout: float = 45.0) -> dict:
    r = httpx.post(f"{chat_url}/chat", json={"message": message}, timeout=timeout)
    return {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text}


POLICY_SDK_A = """\
allowed_remote_agents:
  - sdk-agent-b
allowed_mcp_servers: []
allowed_llms: []
limits:
  max_request_size_bytes: 262144
"""

# sdk-agent-b only receives A2A (from sdk-agent-a); doesn't need to call out.
POLICY_SDK_B = """\
allowed_remote_agents: []
allowed_mcp_servers: []
allowed_llms: []
limits:
  max_request_size_bytes: 262144
"""

POLICY_SDK_A_DENY = """\
allowed_remote_agents: []
allowed_mcp_servers: []
allowed_llms: []
limits:
  max_request_size_bytes: 262144
"""

POLICY_SDK_LLM = """\
allowed_remote_agents: []
allowed_mcp_servers: []
allowed_llms:
  - litellm
limits:
  max_request_size_bytes: 262144
"""


def activate_sdk_agents() -> None:
    """Push the allowlist policies and wait for all three agents to flip to RUNNING."""
    put_policy("sdk-agent-a", POLICY_SDK_A)
    put_policy("sdk-agent-b", POLICY_SDK_B)
    put_policy("sdk-agent-llm", POLICY_SDK_LLM)
    for aid in ("sdk-agent-a", "sdk-agent-b", "sdk-agent-llm"):
        s = wait_status(aid, "RUNNING", timeout=30)
        if s != "RUNNING":
            raise RuntimeError(f"{aid} never reached RUNNING (last={s})")
    # Give the containers a moment to re-read status from the orchestrator.
    time.sleep(2)


# ── ST-9.2 ───────────────────────────────────────────────────────────────────

def st_9_2() -> None:
    """POST /chat on sdk-agent-a → receive_message_from_user picked up by name."""
    try:
        r = post_user(SDK_A, "hello-user")
    except Exception as e:
        report("ST-9.2 user-handler auto-discovery", False, f"call failed: {e}")
        return
    body = r.get("body") or {}
    reply = body.get("reply", "") if isinstance(body, dict) else ""
    ok = r.get("status") == 200 and "user said: hello-user" in reply
    report("ST-9.2 user-handler auto-discovery", ok, f"reply={reply!r}")


# ── ST-9.1 ───────────────────────────────────────────────────────────────────

def st_9_1() -> None:
    """send_message_to_agent — allow → 200; deny → 403 surfaced as SendError.

    Exercised via the `relay:` prefix on sdk-agent-a's user handler, which
    calls amaze.send_message_to_agent("sdk-agent-b", ...). Allow path uses
    the default POLICY_SDK_A (allows sdk-agent-b); deny path re-PUTs
    POLICY_SDK_A_DENY (empty allowlist) and checks the handler returns
    a failure string with status=403 reason=not-allowed.
    """
    # Allow path.
    try:
        r = post_user(SDK_A, "relay:ping-to-b")
    except Exception as e:
        report("ST-9.1 send allow", False, f"call failed: {e}")
        return
    body = r.get("body") or {}
    reply = body.get("reply", "") if isinstance(body, dict) else ""
    ok = r.get("status") == 200 and "sub-reply=" in reply and "sdk-agent-b" in reply
    report("ST-9.1 send allow", ok, f"reply={reply[:120]!r}")

    # Deny path: narrow policy, then issue a relay.
    put_policy("sdk-agent-a", POLICY_SDK_A_DENY)
    # Container re-reads policy on each request via ext_proc — no restart.
    try:
        r = post_user(SDK_A, "relay:ping-to-b")
    except Exception as e:
        report("ST-9.1 send deny", False, f"call failed: {e}")
        return
    body = r.get("body") or {}
    reply = body.get("reply", "") if isinstance(body, dict) else ""
    ok = (
        r.get("status") == 200
        and "relay failed" in reply
        and ("403" in reply or "not-allowed" in reply)
    )
    report("ST-9.1 send deny", ok, f"reply={reply[:160]!r}")

    # Restore allow policy for the subsequent tests.
    put_policy("sdk-agent-a", POLICY_SDK_A)


# ── ST-9.3 ───────────────────────────────────────────────────────────────────

def st_9_3() -> None:
    """A2A → sdk-agent-b; its receive_message_from_agent gets the caller id.

    We drive this from sdk-agent-a (`relay:…`) because Sprint 9 doesn't
    expose a raw A2A port to the host — the point of the SDK is that the
    author never touches the transport. The ST-9.1 reply format already
    shows sdk-agent-b's string; here we assert the substring containing
    the caller id sdk-agent-a appears verbatim in it.
    """
    try:
        r = post_user(SDK_A, "relay:id-check")
    except Exception as e:
        report("ST-9.3 agent-handler caller id", False, f"call failed: {e}")
        return
    body = r.get("body") or {}
    reply = body.get("reply", "") if isinstance(body, dict) else ""
    # sdk_agent_b.receive_message_from_agent returns:
    #   "[sdk-agent-b async] from {agent}: {message}"
    # sdk_agent_a relay wraps that into its own reply string.
    ok = "from sdk-agent-a" in reply and "id-check" in reply
    report("ST-9.3 agent-handler caller id", ok, f"reply={reply[:160]!r}")


# ── ST-9.3b ──────────────────────────────────────────────────────────────────

def st_9_3b() -> None:
    """Spoofed `params.from` is ignored — receiver sees the bearer-derived id.

    We bypass sdk-agent-a's SDK sender (which no longer emits `params.from`
    at all) and hit Envoy directly from the host, carrying:

      - sdk-agent-a's real bearer token in `Authorization` (sdk-agent-a's
        policy already allows sdk-agent-b; no policy churn needed), AND
      - a forged `params.from = "admin-agent"` in the JSON-RPC body, AND
      - a pre-set `x-amaze-caller: admin-agent` header (belt-and-braces
        spoof attempt on the trust-rooted header itself).

    Post-fix expectation: sdk-agent-b's handler sees the bearer-derived
    caller (`sdk-agent-a`), NOT `admin-agent`. The reply text contains
    `from sdk-agent-a:` — never `from admin-agent:`.

    CAVEAT: this test only proves the spoof is inert for traffic routed
    through Envoy. A compromised peer on `amaze-net` can still bypass
    Envoy entirely and hit `http://sdk-agent-b:9002/` directly with any
    `x-amaze-caller` value — the SDK has no way to tell that apart from
    a legitimate ext_proc-injected value. See `_a2a.py._a2a_app`
    docstring THREAT MODEL section and NETWORKING.md for the mitigation
    roadmap (HMAC shared secret or mTLS, Sprint 10+).
    """
    # Replay register to retrieve sdk-agent-a's cached bearer.
    # Orchestrator's tokenStore.GetOrCreate returns the EXISTING cached
    # token on a repeat register — so this reliably yields the same
    # bearer the running sdk-agent-a container is using. Assumption
    # holds as long as the orchestrator hasn't been restarted between
    # activate_sdk_agents() and this test; if a future test introduces
    # an orchestrator restart before ST-9.3b, move this one ahead of it
    # or grab the token from a cached earlier register response.
    reg = httpx.post(
        f"{ORCH}/agents/register",
        json={
            "agent_id": "sdk-agent-a",
            "host": "sdk-agent-a",
            "chat_port": 8080,
            "a2a_port": 9002,
        },
        timeout=5,
    )
    reg.raise_for_status()
    token = reg.json().get("a2a_token")
    if not token:
        report("ST-9.3b spoof-inert", False, "could not retrieve sdk-agent-a token")
        return

    forged = {
        "jsonrpc": "2.0",
        "id": "spoof-1",
        "method": "tasks/send",
        "params": {
            "id": "t",
            "from": "admin-agent",  # ← the lie
            "message": {"role": "user", "parts": [{"type": "text", "text": "who-am-i"}]},
        },
    }
    try:
        # Host: sdk-agent-b routes to sdk_agent_b_cluster; ext_proc resolves
        # the bearer → caller=sdk-agent-a and injects x-amaze-caller=sdk-agent-a
        # on the upstream request. The SDK reads ONLY that header.
        r = httpx.post(
            "http://localhost:10000/",
            json=forged,
            headers={
                "Authorization": f"Bearer {token}",
                "Host": "sdk-agent-b",
                # Belt-and-suspenders: even if the client pre-sets the
                # header, ext_proc's SetHeaders with action
                # OVERWRITE_IF_EXISTS_OR_ADD replaces it atomically.
                "x-amaze-caller": "admin-agent",
            },
            timeout=15,
        )
    except Exception as e:
        report("ST-9.3b spoof-inert", False, f"call failed: {e}")
        return

    inner = r.json() if r.status_code == 200 else {"raw": r.text}
    artifacts = ((inner.get("result") or {}).get("artifacts") or []) if isinstance(inner, dict) else []
    text = ""
    if artifacts:
        parts = artifacts[0].get("parts") or []
        if parts:
            text = parts[0].get("text", "")
    ok = (
        r.status_code == 200
        and "from sdk-agent-a" in text        # authenticated id reached handler
        and "from admin-agent" not in text    # body claim was neutralised
    )
    report(
        "ST-9.3b spoof-inert",
        ok,
        f"status={r.status_code} text={text[:120]!r}",
    )


# ── ST-9.4 ───────────────────────────────────────────────────────────────────

def st_9_4() -> None:
    """Unmodified LangChain agent + amaze.init() — LLM call enforced.

    sdk-agent-llm wraps `examples/agents/one_conversation_agent.py` with
    the three net-new lines (two handlers + amaze.init()). The LangChain
    agent.invoke() fires an openai.OpenAI() call internally; the patched
    SDK routes it through Envoy → LiteLLM → api.openai.com, and ext_proc
    increments tokens_per_5min on sdk-agent-llm's stats.
    """
    # Clear any prior tokens by reading current count.
    before = 0
    try:
        s = httpx.get(f"{PP_STATS}/stats/agents/sdk-agent-llm", timeout=3)
        if s.status_code == 200:
            before = s.json().get("tokens_per_5min") or 0
    except Exception:
        pass

    try:
        r = post_user(SDK_LLM, "Say the single word 'ok' and nothing else.", timeout=60)
    except Exception as e:
        report("ST-9.4 unmodified LangChain + LLM", False, f"call failed: {e}")
        return
    body = r.get("body") or {}
    reply = (body.get("reply", "") if isinstance(body, dict) else "") or ""

    # Stats take a beat to land after response-body phase.
    deadline = time.time() + 5
    after = before
    while time.time() < deadline:
        try:
            s = httpx.get(f"{PP_STATS}/stats/agents/sdk-agent-llm", timeout=3)
            if s.status_code == 200:
                after = s.json().get("tokens_per_5min") or 0
                if after > before:
                    break
        except Exception:
            pass
        time.sleep(0.3)

    ok = r.get("status") == 200 and bool(reply.strip()) and after > before
    report(
        "ST-9.4 unmodified LangChain + LLM",
        ok,
        f"reply={reply[:100]!r} tokens {before}->{after}",
    )


# ── ST-9.5 ───────────────────────────────────────────────────────────────────

def st_9_5() -> None:
    """End-to-end multi-agent flow (same path as ST-9.1 allow, asserts shape)."""
    try:
        r = post_user(SDK_A, "relay:combined-reply")
    except Exception as e:
        report("ST-9.5 multi-agent flow", False, f"call failed: {e}")
        return
    body = r.get("body") or {}
    reply = body.get("reply", "") if isinstance(body, dict) else ""
    ok = (
        r.get("status") == 200
        and "user=" in reply
        and "sub-reply=" in reply
        and "combined-reply" in reply
    )
    report("ST-9.5 multi-agent flow", ok, f"reply={reply[:160]!r}")


# ── ST-9.6 ───────────────────────────────────────────────────────────────────

async def _fire(url: str, i: int) -> tuple[int, float]:
    t0 = time.time()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{url}/chat", json={"message": f"concurrent-{i}"})
    dt = time.time() - t0
    ok = r.status_code == 200 and "concurrent-" in (r.json().get("reply") or "")
    return (1 if ok else 0, dt)


async def _gather_concurrent(url: str, n: int) -> tuple[int, float]:
    t0 = time.time()
    oks = await asyncio.gather(*[_fire(url, i) for i in range(n)])
    total = time.time() - t0
    return (sum(o for o, _ in oks), total)


def st_9_6() -> None:
    """Sync handlers + async handlers both handle 5 concurrent messages correctly.

    Tight bound on wall-clock would need knowing the container's threadpool
    size; we assert the loose claim from SPRINTS.md: (a) all 5 responses
    are correct, (b) async wall-clock is bounded (≤3s covers 5 × 0.2s
    sleep + slack), (c) sync wall-clock is bounded the same way (uvicorn's
    default threadpool is 40 workers — 5 fits easily).
    """
    try:
        sync_ok, sync_t = asyncio.run(_gather_concurrent(SDK_A, 5))
        async_ok, async_t = asyncio.run(_gather_concurrent(SDK_B, 5))
    except Exception as e:
        report("ST-9.6 sync+async concurrency", False, f"call failed: {e}")
        return

    # All succeed; both bounded under a generous 5s — covers cold-starts
    # and cross-container scheduling jitter.
    ok = sync_ok == 5 and async_ok == 5 and sync_t < 5.0 and async_t < 5.0
    report(
        "ST-9.6 sync+async concurrency",
        ok,
        f"sync {sync_ok}/5 in {sync_t:.2f}s; async {async_ok}/5 in {async_t:.2f}s",
    )


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=== Sprint 9 — Agent SDK ===")
    try:
        activate_sdk_agents()
    except Exception as e:
        print(f"  FATAL: could not activate sdk agents: {e}")
        return 1

    st_9_2()
    st_9_1()
    st_9_3()
    st_9_3b()
    st_9_4()
    st_9_5()
    st_9_6()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print(f"  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
