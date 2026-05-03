"""amaze — minimal Python Agent SDK (Sprint 9).

Public surface:

    # Send to another agent through the aMaze control plane.
    # message and reply can be a str, dict, list, int, float, bool, or None —
    # any JSON-serializable value. Strings are the common case and work
    # exactly as before; structured types are encoded transparently on the wire.
    reply = amaze.send_message_to_agent("target-agent", "hello")
    reply = amaze.send_message_to_agent("target-agent", {"action": "search", "q": "..."})

    # Define one or both handlers in your module, then call init().
    def receive_message_from_user(message):
        return f"you said: {message}"

    def receive_message_from_agent(agent, message):
        return f"from {agent}: {message}"

    amaze.init()          # blocks; runs the A2A + chat servers
    # ... or for an async-first agent:
    async def receive_message_from_user(message):
        return await agent.ainvoke(message)

`init()` resolves both handlers in the caller's own module globals — the
author doesn't pass anything to the SDK. LLM and MCP traffic are handled
by the Sprint-8 patched SDKs in `sdk_patches/`, so this package
deliberately does NOT try to wrap them.
"""

from __future__ import annotations

import sys
from typing import Any

# Apply provider-SDK monkey-patches on `import amaze` — this must happen
# before the author instantiates any LLM client (openai, anthropic, …)
# so the patches replace `__init__` ahead of first use. `sdk_patches` is
# an optional sibling package that ships alongside the SDK in container
# images; if it isn't on PYTHONPATH (e.g. a host-side unit test) the
# import silently fails and plain openai/anthropic behaviour is left
# untouched — the agent just won't have enforcement on LLM traffic.
try:
    import sdk_patches  # noqa: F401  (side-effect import)
except Exception:
    pass

from . import _a2a, _core, _handlers
from ._a2a import SendError

__all__ = ["init", "send_message_to_agent", "SendError"]


def init(
    agent_id: str | None = None,
    *,
    block: bool = True,
    on_startup: "Any | None" = None,
) -> None:
    """Bootstrap the SDK — register, start servers, dispatch handlers.

    Parameters
    ----------
    agent_id:
        Optional override for the agent id. Defaults to `$AMAZE_AGENT_ID`.
    block:
        When True (default), runs the servers in the foreground and never
        returns — this is what an agent's `__main__` script wants. When
        False, spawns them on a background thread so integration tests
        can drive the process and tear it down manually.
    on_startup:
        Optional async callable (no arguments) that runs once, immediately
        after the agent registers and receives its bearer token. Use it to
        pre-build LangChain agents, fetch MCP tool lists, or warm any other
        async resource before the first request arrives.

        Example — eager MCP agent build::

            async def startup():
                await _build_agent()

            amaze.init(on_startup=startup)

        MCP tool discovery (tools/list, initialize) is allowed by the proxy
        even before a policy is pushed, so this call will always succeed at
        startup. Actual tool *calls* still require a policy.
    """
    _core.load(agent_id)
    # Walk the caller's globals so authors don't have to register handlers
    # explicitly. Depth=1 lands us in the module that called init().
    caller_globals: dict[str, Any] = sys._getframe(1).f_globals
    _handlers.register(caller_globals)
    _a2a.start_server(block=block, on_startup=on_startup)


def send_message_to_agent(target: str, message: Any) -> Any:
    """Send `message` to `target` (an agent_id) and return the reply.

    `message` can be any JSON-serializable value: str, dict, list, int,
    float, bool, or None. The reply has the same range of types — whatever
    the receiving agent's handler returns.

    Blocking — safe to call from sync handlers. Raises `SendError` on
    policy denies, partner errors, or missing bearer.
    """
    return _a2a.send_sync(target, message)
