"""Handler discovery + sync/async dispatch.

The SDK offers a zero-argument `amaze.init()` for the agent author. We
resolve the two handler functions by name in the caller's own module
globals — that's why `init()` walks `sys._getframe(1).f_globals`. This
keeps the SDK surface three functions instead of introducing a
registration API that authors have to wire up explicitly.

Sync vs async: `asyncio.iscoroutinefunction` decides whether we await
the handler directly (async path — cheap, high-concurrency) or run it
in the default threadpool (sync path — safe for LangChain / CrewAI /
blocking SDKs without forcing authors to rewrite their code).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

_user_handler: Callable[..., Any] | None = None
_agent_handler: Callable[..., Any] | None = None


class HandlerMissing(RuntimeError):
    """The expected handler function isn't defined in the caller's module."""


def register(globals_dict: dict[str, Any]) -> None:
    """Install the two handlers by looking them up by name.

    Called from `amaze.init()` with the caller's `f_globals`. Missing
    handlers are allowed — we only raise when a request actually tries
    to dispatch to the missing one, so an agent that only talks to users
    doesn't need to implement `receive_message_from_agent` and vice versa.
    """
    global _user_handler, _agent_handler
    _user_handler = globals_dict.get("receive_message_from_user")
    _agent_handler = globals_dict.get("receive_message_from_agent")


async def call_user_handler(message: Any) -> Any:
    if _user_handler is None:
        raise HandlerMissing(
            "define `receive_message_from_user(message)` in your module"
        )
    return await _dispatch(_user_handler, message)


async def call_agent_handler(caller_id: str, message: Any) -> Any:
    if _agent_handler is None:
        raise HandlerMissing(
            "define `receive_message_from_agent(agent, message)` in your module"
        )
    return await _dispatch(_agent_handler, caller_id, message)


async def _dispatch(fn: Callable[..., Any], *args: Any) -> Any:
    """Await coroutine handlers on the loop; run sync handlers in threadpool.

    Returning from a sync handler blocks a threadpool worker but not the
    event loop, which is exactly the contract the SDK documents. Returns
    the handler's value as-is; None is normalised to "" so callers don't
    have to guard against it.
    """
    if asyncio.iscoroutinefunction(fn):
        result = await fn(*args)
    else:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, fn, *args)
    return result if result is not None else ""
