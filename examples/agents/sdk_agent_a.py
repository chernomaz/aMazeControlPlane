"""sdk-agent-a — Sprint 9 demo agent (sync handlers).

Minimal author-written code using the amaze SDK. Both handlers are
synchronous `def` functions — the SDK dispatches them in a threadpool
so long-running sync calls (think LangChain `.invoke()`) don't block
the event loop.

Inbound A2A is echoed back with a tag identifying the caller; user
messages are echoed with the agent id prefix. For ST-9.5 the user
handler also demonstrates `send_message_to_agent` — it forwards the
message to `sdk-agent-b` and returns the combined reply.
"""

from __future__ import annotations

import os

import amaze


TARGET_B = os.environ.get("AMAZE_SUB_AGENT", "sdk-agent-b")


def receive_message_from_user(message: str) -> str:
    # ST-9.5 end-to-end: fan out to another agent and include its reply
    # in our own response. Guarded on an env flag so ST-9.2 can test the
    # "no outbound send" path without agent-b being involved.
    if message.startswith("relay:"):
        payload = message[len("relay:"):]
        try:
            sub = amaze.send_message_to_agent(TARGET_B, payload)
        except amaze.SendError as e:
            return f"[sdk-agent-a] relay failed: status={e.status_code} reason={e.reason}"
        return f"[sdk-agent-a] user={payload!r}  sub-reply={sub!r}"
    return f"[sdk-agent-a] user said: {message}"


def receive_message_from_agent(agent: str, message: str) -> str:
    return f"[sdk-agent-a] from {agent}: {message}"


if __name__ == "__main__":
    amaze.init()
