"""Agent-runtime SDK monkey-patches — Sprint 8 Phase 8B.

Importing this package (`import sdk_patches`) applies provider-SDK
patches that rewrite outbound calls to go through the local Envoy proxy,
so the enforcement layer sees the request/response bodies and can (a)
check the agent's allow-list and (b) extract `usage.total_tokens`.

The agent author's source code is **not** modified — they keep writing
`openai.OpenAI()` / `anthropic.Anthropic()` as usual. The patch silently:

  * sets `base_url=http://envoy:10000` (plain HTTP — no CONNECT tunnel)
  * adds `Host: litellm` so Envoy routes to the LiteLLM sidecar
  * adds `x-agent-id: $AMAZE_AGENT_ID` so ext_proc can identify the caller

Patches are applied idempotently — calling `apply()` twice is a no-op.
Missing SDKs (openai not installed, anthropic not installed) are skipped
silently so the image stays usable when an agent only needs one provider.
"""

from __future__ import annotations

from . import openai_patch, anthropic_patch


def apply() -> None:
    openai_patch.apply()
    anthropic_patch.apply()


# Applied at import time so `import sdk_patches` in the entrypoint is
# all that's needed before the user's agent module runs.
apply()
