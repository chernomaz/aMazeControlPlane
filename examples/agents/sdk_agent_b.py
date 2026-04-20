"""sdk-agent-b — Sprint 9 demo agent (async handlers).

Same contract as sdk_agent_a but defined with `async def`. The SDK
inspects each handler via `asyncio.iscoroutinefunction` and awaits
coroutines directly on the event loop — so N concurrent A2A requests
fan out without being serialised through a threadpool.

ST-9.6 fires 5 concurrent messages at each agent; the async agent's
wall-clock should reflect event-loop concurrency (~max single-call
latency), whereas sdk-agent-a's sync handlers are gated by the
threadpool size (uvicorn's default, typically 40+ workers — easily
enough for 5, but the test only asserts correctness + bounded latency).
"""

from __future__ import annotations

import asyncio


async def receive_message_from_user(message: str) -> str:
    # A small await so a handler that legitimately does async I/O
    # (e.g. httpx.AsyncClient) isn't optimised away — the test asserts
    # per-call latency shape, not zero-latency.
    await asyncio.sleep(0.2)
    return f"[sdk-agent-b async] user said: {message}"


async def receive_message_from_agent(agent: str, message: str) -> str:
    await asyncio.sleep(0.2)
    return f"[sdk-agent-b async] from {agent}: {message}"


if __name__ == "__main__":
    import amaze

    amaze.init()
