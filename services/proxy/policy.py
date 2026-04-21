"""
Policy loader + evaluation. In-process; no HTTP hop to a separate service.

Why in-process: eliminates aMaze's audit-flagged "fails open" bug — there's
no separate service to be unreachable. A raise inside the enforcer is caught
at the mitmproxy addon boundary and turned into a 403 (fail closed).

policies.yaml is read once at proxy boot. Reload requires a proxy restart
(step 1 simplification; dynamic reload is a later sprint).
"""
from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Policy:
    agent_id: str
    allowed_remote_agents: list[str] = field(default_factory=list)
    allowed_mcp_servers: list[str] = field(default_factory=list)
    # { mcp_server_name: [tool_names] }
    allowed_tools: dict[str, list[str]] = field(default_factory=dict)
    # [{provider, models: [...]}]
    allowed_llms: list[dict[str, Any]] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)


class Policies:
    """Container keyed by agent_id. Missing key = fail-closed deny."""

    def __init__(self, by_agent: dict[str, Policy]) -> None:
        self._by_agent = by_agent

    def get(self, agent_id: str) -> Policy | None:
        return self._by_agent.get(agent_id)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._by_agent


def load_policies(path: pathlib.Path) -> Policies:
    """Parse policies.yaml into a Policies container.

    Structure expected:
        policies:
          <agent_id>:
            allowed_remote_agents: [...]
            allowed_mcp_servers: [...]
            allowed_tools: { server: [tool, ...] }
            allowed_llms: [{provider, models}]
            limits: {...}
    """
    if not path.exists():
        logger.warning("policies.yaml not found at %s — all requests will deny", path)
        return Policies({})

    raw = yaml.safe_load(path.read_text()) or {}
    entries = (raw.get("policies") or {})
    by_agent: dict[str, Policy] = {}
    for agent_id, spec in entries.items():
        spec = spec or {}
        by_agent[agent_id] = Policy(
            agent_id=agent_id,
            allowed_remote_agents=list(spec.get("allowed_remote_agents") or []),
            allowed_mcp_servers=list(spec.get("allowed_mcp_servers") or []),
            allowed_tools={
                k: list(v or []) for k, v in (spec.get("allowed_tools") or {}).items()
            },
            allowed_llms=list(spec.get("allowed_llms") or []),
            limits=dict(spec.get("limits") or {}),
        )
    logger.info("loaded %d policies from %s", len(by_agent), path)
    return Policies(by_agent)


# --- classification -------------------------------------------------------

LLM_HOSTS: set[str] = {
    "api.openai.com",
    "api.anthropic.com",
}


def is_llm_host(host: str) -> bool:
    """True if `host` is a known LLM provider endpoint."""
    return host in LLM_HOSTS


def llm_model_allowed(policy: Policy, provider: str, model: str) -> bool:
    """Check a specific provider+model against the agent's allowed_llms list.

    Matches if there is any entry `{provider: p}` where p equals `provider`
    and `model` is in that entry's `models` list.
    """
    for entry in policy.allowed_llms:
        if entry.get("provider") != provider:
            continue
        if model in (entry.get("models") or []):
            return True
    return False


def host_to_provider(host: str) -> str | None:
    """Map a well-known LLM host back to its provider name (for policy matching)."""
    if host == "api.openai.com":
        return "openai"
    if host == "api.anthropic.com":
        return "anthropic"
    return None
