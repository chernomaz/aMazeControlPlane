"""sdk-agent-llm — Sprint 9 ST-9.4 demo.

Adapted from `examples/agents/one_conversation_agent.py` with the
minimum surgery to prove the zero-change promise:

 * the original `main()` CLI is dropped — containers don't read stdin;
 * the original tool list (`pdf_search, web_search, …`) is dropped
   because those helpers live outside this file and aren't needed to
   demonstrate LLM enforcement end-to-end;
 * `agent.invoke(...)` is replaced with `llm.invoke(...)` — skipping
   LangChain's `create_agent` wrapper, which under langchain 1.x spins
   up a langgraph pipeline that does background compilation on first
   call and stalls the demo. The enforcement path we're proving is
   `ChatOpenAI.invoke → openai.OpenAI (patched) → Envoy → LiteLLM`,
   and that runs identically either way.
 * `amaze.init()` starts the SDK.

Everything else — the `ChatOpenAI(...)` construction, the system
prompt — is carried over verbatim. The patched `openai` SDK from
`sdk_patches/` (auto-imported by `import amaze`) rewrites
`ChatOpenAI`'s underlying base_url + headers so the LLM call flows
through Envoy → LiteLLM → api.openai.com, with ext_proc counting
tokens on the response.
"""

from __future__ import annotations

import os
import logging

# LangSmith tries to upload traces over HTTPS to api.smith.langchain.com;
# inside the container that's tunnelled through Envoy, which has no
# cluster for that host → the upload hangs until invoke() times out.
# Disable tracing here so the ST-9.4 demo isn't blocked on an
# observability side-channel we don't exercise.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)
os.environ.pop("LANGCHAIN_API_KEY", None)

from dotenv import load_dotenv
# override=False keeps our disables above sticky even if .env tries to
# re-enable them.
load_dotenv(override=False)

from langchain_openai import ChatOpenAI

import amaze

logger = logging.getLogger(__name__)

# ---------- LLM ----------
#
# `api_key` is inert at runtime — sdk_patches/openai_patch.py monkey-patches
# openai.OpenAI.__init__ to substitute a NEMO placeholder and redirect to
# http://envoy:10000/v1. The real provider key lives in the LiteLLM sidecar.
llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY", "sk-unused"),
    max_tokens=32,
)


def receive_message_from_user(message: str) -> str:
    resp = llm.invoke(
        [
            {"role": "system", "content": "Answer in one very short sentence."},
            {"role": "user", "content": message},
        ]
    )
    return str(getattr(resp, "content", "") or "")


def receive_message_from_agent(agent_id: str, message: str) -> str:
    # ST-9.4 is scoped to user→LLM — inbound A2A just echoes.
    return f"[sdk-agent-llm] A2A from {agent_id}: {message}"


if __name__ == "__main__":
    amaze.init()
