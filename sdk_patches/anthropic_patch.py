"""Monkey-patch `anthropic.Anthropic.__init__` — mirrors openai_patch.py.

LiteLLM accepts Anthropic messages at /v1/messages. The patched SDK
sends plain HTTP to Envoy with Host: litellm; Envoy forwards to the
LiteLLM sidecar; LiteLLM opens TLS to api.anthropic.com.
"""

from __future__ import annotations

import os

_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return

    try:
        import anthropic
    except ImportError:
        return

    proxy = os.environ.get("AMAZE_PROXY")
    agent_id = os.environ.get("AMAZE_AGENT_ID")
    if not proxy or not agent_id:
        return

    base_url = proxy.rstrip("/") + "/"

    original_init = anthropic.Anthropic.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-redef]
        # Overwrite (don't setdefault) to survive wrappers that pass
        # `base_url=None` — same reasoning as openai_patch.
        kwargs["api_key"] = kwargs.get("api_key") or "sk-nemo-placeholder"
        kwargs["base_url"] = base_url
        default_headers = dict(kwargs.get("default_headers") or {})
        default_headers.setdefault("host", "litellm")
        default_headers.setdefault("x-agent-id", agent_id)
        kwargs["default_headers"] = default_headers
        return original_init(self, *args, **kwargs)

    anthropic.Anthropic.__init__ = patched_init  # type: ignore[method-assign]
    _applied = True
