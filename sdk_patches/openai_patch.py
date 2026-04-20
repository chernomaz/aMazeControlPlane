"""Monkey-patch `openai.OpenAI.__init__` to route all calls through NEMO.

After the patch, the author-written code

    client = openai.OpenAI()
    client.chat.completions.create(model="gpt-4o-mini", messages=[...])

becomes equivalent to

    openai.OpenAI(
        api_key="dummy",  # real key lives in the LiteLLM sidecar
        base_url=f"{AMAZE_PROXY}/",
        default_headers={
            "host": "litellm",
            "x-agent-id": AMAZE_AGENT_ID,
        },
    )

without requiring a single change to the agent source. The request goes
plain HTTP to Envoy; Envoy forwards to the LiteLLM sidecar; LiteLLM opens
TLS to the real provider. ext_proc sees the request + response plaintext.

`api_key` is set to a sentinel string because the `openai` SDK raises
unless one is present — LiteLLM is the real authenticator and uses the
provider key baked into its container env (`OPENAI_API_KEY`, etc.).
"""

from __future__ import annotations

import os

_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return

    try:
        import openai
    except ImportError:  # SDK not installed → nothing to patch
        return

    proxy = os.environ.get("AMAZE_PROXY")
    agent_id = os.environ.get("AMAZE_AGENT_ID")
    if not proxy or not agent_id:
        # If we're not inside a NEMO container, don't touch the SDK.
        return

    # LiteLLM's OpenAI-compat surface is at /v1/... The SDK appends the
    # resource path (e.g. `/chat/completions`) to base_url verbatim, and
    # its own default is `https://api.openai.com/v1` — so we need the
    # /v1 suffix here as well.
    base_url = proxy.rstrip("/") + "/v1"

    original_init = openai.OpenAI.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-redef]
        # Overwrite base_url — plain setdefault fails when wrappers like
        # langchain-openai pass `base_url=None` explicitly. Without the
        # forced rewrite the openai client falls back to api.openai.com,
        # which routes via HTTPS_PROXY=envoy and blocks as a CONNECT
        # tunnel (Envoy has no cluster for the real OpenAI domain).
        # Same reasoning for api_key: honour a real user-supplied key but
        # never let None or "" through to the validation inside openai.
        kwargs["api_key"] = kwargs.get("api_key") or "sk-nemo-placeholder"
        kwargs["base_url"] = base_url
        default_headers = dict(kwargs.get("default_headers") or {})
        default_headers.setdefault("host", "litellm")
        default_headers.setdefault("x-agent-id", agent_id)
        kwargs["default_headers"] = default_headers
        return original_init(self, *args, **kwargs)

    openai.OpenAI.__init__ = patched_init  # type: ignore[method-assign]
    _applied = True
