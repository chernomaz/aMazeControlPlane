#!/usr/bin/env python3
"""Sprint 7 system tests — Statistics Collection.

ST-7.1  Request counts tracked: 3 allowed A2A + 1 denied →
        /stats/agents/agent-a shows requests_allowed=3, requests_denied=1.
ST-7.2  Tool call counts tracked: 2 raw tools/call for echo →
        /stats/tools/echo shows calls=2.
ST-7.3  LLM response time recorded: after LLM call →
        /stats/agents/agent-llm-test avg_response_time_ms > 0.
ST-7.4  Tool response time recorded: after echo calls from ST-7.2 →
        /stats/tools/echo avg_response_time_ms > 0.
ST-7.5  Token counts tracked: after LLM call from ST-7.3 →
        /stats/agents/agent-llm-test tokens_per_5min > 0.

Prerequisites: run_sprint7.sh already started the stack.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

ENVOY = "http://localhost:10000"
STATS = "http://localhost:8081"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


# ── helpers ───────────────────────────────────────────────────────────────────

def a2a(caller: str, target: str) -> httpx.Response:
    return httpx.post(
        ENVOY,
        headers={"host": target, "x-agent-id": caller, "content-type": "application/json"},
        json={"jsonrpc": "2.0", "method": "tasks/send",
              "params": {"message": "ping"}, "id": 1},
        timeout=3,
    )


def mcp_tools_call(tool: str, args: dict, agent_id: str = "agent-a-mcp") -> httpx.Response:
    """Raw tools/call — ext_proc enforces and records; mcp-server may return 400 (no session)."""
    return httpx.post(
        ENVOY,
        headers={"host": "mcp-server", "x-agent-id": agent_id,
                 "content-type": "application/json"},
        json={"jsonrpc": "2.0", "method": "tools/call",
              "params": {"name": tool, "arguments": args}, "id": 1},
        timeout=5,
    )


def llm_call(agent_id: str = "agent-llm-test") -> httpx.Response:
    return httpx.post(
        f"{ENVOY}/v1/chat/completions",
        headers={
            "host": "openai-api",
            "x-agent-id": agent_id,
            "authorization": f"Bearer {OPENAI_KEY}",
            "content-type": "application/json",
        },
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": "Say hi in one word"}],
              "max_tokens": 5},
        timeout=30,
    )


def agent_stats(agent_id: str) -> dict:
    r = httpx.get(f"{STATS}/stats/agents/{agent_id}", timeout=5)
    assert r.status_code == 200, f"stats 404 for {agent_id}: {r.text}"
    return r.json()


def tool_stats(tool_name: str) -> dict:
    r = httpx.get(f"{STATS}/stats/tools/{tool_name}", timeout=5)
    assert r.status_code == 200, f"stats 404 for tool {tool_name}: {r.text}"
    return r.json()


# ── tests ─────────────────────────────────────────────────────────────────────

def test_st_7_1():
    # 3 allowed: agent-a → agent-b (in allowlist; upstream may 502 but ext_proc marks allowed)
    for _ in range(3):
        try:
            a2a("agent-a", "agent-b")
        except Exception:
            pass  # timeout or 502 from Envoy is fine — ext_proc still records

    # 1 denied: agent-a → agent-c (not in allowlist → ext_proc denies before routing)
    r = a2a("agent-a", "agent-c")
    assert r.status_code == 403, f"Expected 403 for denied A2A, got {r.status_code}"

    snap = agent_stats("agent-a")
    assert snap["requests_allowed"] == 3, \
        f"Expected requests_allowed=3, got {snap['requests_allowed']}"
    assert snap["requests_denied"] == 1, \
        f"Expected requests_denied=1, got {snap['requests_denied']}"
    print(f"  [{PASS}] ST-7.1 — agent-a: allowed=3, denied=1")


def test_st_7_2():
    # 2 raw tools/call for echo (allowed by policy; mcp-server may return 400 — that's OK)
    for _ in range(2):
        mcp_tools_call("echo", {"text": "hello"})

    snap = tool_stats("echo")
    assert snap["calls"] == 2, f"Expected echo calls=2, got {snap['calls']}"
    print(f"  [{PASS}] ST-7.2 — echo tool: calls=2")


def test_st_7_3():
    if not OPENAI_KEY:
        print(f"  [SKIP] ST-7.3 — OPENAI_API_KEY not set")
        return
    r = llm_call()
    assert r.status_code == 200, f"LLM call failed: {r.status_code} {r.text[:200]}"

    snap = agent_stats("agent-llm-test")
    assert snap["avg_response_time_ms"] > 0, \
        f"Expected avg_response_time_ms > 0, got {snap['avg_response_time_ms']}"
    print(f"  [{PASS}] ST-7.3 — agent-llm-test: avg_response_time_ms="
          f"{snap['avg_response_time_ms']:.1f}ms")


def test_st_7_4():
    snap = tool_stats("echo")
    assert snap["avg_response_time_ms"] > 0, \
        f"Expected echo avg_response_time_ms > 0, got {snap['avg_response_time_ms']}"
    print(f"  [{PASS}] ST-7.4 — echo tool: avg_response_time_ms="
          f"{snap['avg_response_time_ms']:.1f}ms")


def test_st_7_5():
    snap = agent_stats("agent-llm-test")
    assert snap["tokens_per_5min"] > 0, \
        f"Expected tokens_per_5min > 0, got {snap['tokens_per_5min']}"
    print(f"  [{PASS}] ST-7.5 — agent-llm-test: tokens_per_5min={snap['tokens_per_5min']}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Sprint 7 System Tests ===\n")
    failed = 0
    for fn in [test_st_7_1, test_st_7_2, test_st_7_3, test_st_7_4, test_st_7_5]:
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
    print("All Sprint 7 tests passed.")


if __name__ == "__main__":
    main()
