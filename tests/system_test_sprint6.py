#!/usr/bin/env python3
"""Sprint 6 system tests — Per-agent Token Limits.

ST-6.1  First real LLM call through Envoy succeeds (200 + valid completion).
ST-6.2  Second call is denied with 429 token-limit-exceeded (first call
        consumed > 10 tokens, which is the cap for agent-llm-test).
ST-6.3  After the 10-second window elapses the same agent can call again.
ST-6.4  A2A traffic does not consume the token budget:
        agent-llm-test makes an A2A attempt (denied by policy, not by tokens),
        then its LLM call succeeds — the A2A deny did not touch the tracker.

Prerequisites: run_sprint6.sh already started the stack.
"""

import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

ENVOY = "http://localhost:10000"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
WINDOW_SECONDS = 10  # must match agents.yaml rate_window_seconds for agent-llm-test

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


# ── helpers ───────────────────────────────────────────────────────────────────

def llm_call(agent_id: str = "agent-llm-test") -> httpx.Response:
    return httpx.post(
        f"{ENVOY}/v1/chat/completions",
        headers={
            "host": "openai-api",
            "x-agent-id": agent_id,
            "authorization": f"Bearer {OPENAI_KEY}",
            "content-type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Say hi in one word"}],
            "max_tokens": 5,
        },
        timeout=30,
    )


def a2a_call(caller: str, target: str) -> httpx.Response:
    return httpx.post(
        ENVOY,
        headers={
            "host": target,
            "x-agent-id": caller,
            "content-type": "application/json",
        },
        json={"jsonrpc": "2.0", "method": "tasks/send",
              "params": {"message": "ping"}, "id": 1},
        timeout=10,
    )


def is_token_denied(resp: httpx.Response) -> bool:
    if resp.status_code != 429:
        return False
    try:
        return resp.json().get("reason") == "token-limit-exceeded"
    except Exception:
        return False


def wait_for_window():
    print(f"    (waiting {WINDOW_SECONDS + 2}s for token window to reset…)")
    time.sleep(WINDOW_SECONDS + 2)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_st_6_1():
    resp = llm_call()
    assert resp.status_code == 200, \
        f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    assert "choices" in data, f"No 'choices' in response: {data}"
    tokens = data.get("usage", {}).get("total_tokens", 0)
    print(f"  [{PASS}] ST-6.1 — first LLM call succeeded "
          f"(total_tokens={tokens}, well above 10-token cap)")


def test_st_6_2():
    resp = llm_call()
    assert is_token_denied(resp), \
        f"Expected 429 token-limit-exceeded, got {resp.status_code}: {resp.text[:300]}"
    print(f"  [{PASS}] ST-6.2 — second LLM call denied with token-limit-exceeded")


def test_st_6_3():
    wait_for_window()
    resp = llm_call()
    assert resp.status_code == 200, \
        f"Expected 200 after window reset, got {resp.status_code}: {resp.text[:300]}"
    print(f"  [{PASS}] ST-6.3 — LLM call succeeds after window reset")


def test_st_6_4():
    # Fresh window needed; ST-6.3 consumed tokens so wait again.
    wait_for_window()

    # agent-llm-test has no allowed_remote_agents → A2A denied by policy (not tokens)
    a2a_resp = a2a_call("agent-llm-test", "agent-b")
    assert a2a_resp.status_code == 403, \
        f"Expected A2A to be policy-denied (403), got {a2a_resp.status_code}"
    assert a2a_resp.json().get("reason") == "not-allowed", \
        f"Expected reason=not-allowed, got {a2a_resp.json()}"

    # Token window is still clean — A2A denial must not have touched the tracker
    llm_resp = llm_call()
    assert llm_resp.status_code == 200, \
        f"Expected 200 (A2A should not consume token budget), " \
        f"got {llm_resp.status_code}: {llm_resp.text[:300]}"
    print(f"  [{PASS}] ST-6.4 — A2A traffic did not consume token budget; "
          f"subsequent LLM call succeeded")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not OPENAI_KEY:
        print("ERROR: OPENAI_API_KEY not set in .env")
        sys.exit(1)

    print("\n=== Sprint 6 System Tests ===\n")
    failed = 0
    for fn in [test_st_6_1, test_st_6_2, test_st_6_3, test_st_6_4]:
        try:
            fn()
        except AssertionError as e:
            print(f"  [{FAIL}] {fn.__name__} — {e}")
            failed += 1
        except Exception as e:
            print(f"  [{FAIL}] {fn.__name__} — unexpected error: {e}")
            failed += 1

    print()
    if failed:
        print(f"{failed} test(s) FAILED")
        sys.exit(1)
    print("All Sprint 6 tests passed.")


if __name__ == "__main__":
    main()
