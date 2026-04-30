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
import re

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema (pydantic v2 — extra="forbid" everywhere for fail-loud validation)
# ---------------------------------------------------------------------------

class RateLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: str        # e.g. "10m", "1h", "30s"
    max_tokens: int

    @field_validator("window")
    @classmethod
    def _validate_window(cls, v: str) -> str:
        if not re.fullmatch(r"\d+[smh]", v):
            raise ValueError(
                f"invalid window {v!r}: must be a positive integer followed by s/m/h"
            )
        return v


class GraphStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: int
    call_type: str     # "tool" | "agent"
    callee_id: str
    max_loops: int = 1
    next_steps: list[int] = []


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_step: int
    steps: list[GraphStep]

    @model_validator(mode="after")
    def _validate_step_refs(self) -> "Graph":
        step_ids = {s.step_id for s in self.steps}
        if self.start_step not in step_ids:
            raise ValueError(
                f"start_step {self.start_step} not in step_ids {sorted(step_ids)}"
            )
        for step in self.steps:
            for next_id in step.next_steps:
                if next_id not in step_ids:
                    raise ValueError(
                        f"step {step.step_id}: next_steps references "
                        f"non-existent step_id {next_id}"
                    )
        return self


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    max_tokens_per_turn: int = 0          # 0 = unlimited
    max_tool_calls_per_turn: int = 0      # 0 = unlimited
    max_agent_calls_per_turn: int = 0     # 0 = unlimited
    allowed_llm_providers: list[str] = []
    token_rate_limits: list[RateLimit] = []
    on_budget_exceeded: str = "block"     # "block" | "allow"
    on_violation: str = "block"           # "block" | "allow"
    mode: str = "flexible"                # "strict" | "flexible"
    allowed_tools: list[str] = []         # flat list (flexible mode)
    allowed_agents: list[str] = []        # flat list (flexible and strict mode)
    graph: Graph | None = None            # strict mode only


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_policies(path: pathlib.Path) -> dict[str, Policy]:
    """Parse policies.yaml into a dict keyed by agent_id (== name field).

    Structure expected:
        policies:
          <agent_id>:
            name: <agent_id>    # must match the key
            ...
    """
    if not path.exists():
        logger.warning("policies.yaml not found at %s — all requests will deny", path)
        return {}

    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("policies") or {}
    by_agent: dict[str, Policy] = {}
    for agent_id, spec in entries.items():
        spec = dict(spec or {})
        # Inject `name` from the YAML key so callers don't have to repeat it,
        # but honour an explicit name field if present (they must agree).
        spec.setdefault("name", agent_id)
        policy = Policy.model_validate(spec)
        if policy.name != agent_id:
            raise ValueError(
                f"Policy key '{agent_id}' does not match name field '{policy.name}'"
            )
        by_agent[agent_id] = policy
    logger.info("loaded %d policies from %s", len(by_agent), path)
    return by_agent


def load_policies_from_yaml(path: str | pathlib.Path = "config/policies.yaml") -> dict[str, Policy]:
    """Public wrapper around `load_policies` accepting str or Path.

    Used by `policy_store.bootstrap_from_yaml` and the YAML-fallback in
    `policy_store.get_policy`. Returns `{}` if the file is missing — the
    caller decides whether that's an error.
    """
    return load_policies(pathlib.Path(path))


# ---------------------------------------------------------------------------
# Classification helpers (unchanged)
# ---------------------------------------------------------------------------

LLM_HOSTS: set[str] = {
    "api.openai.com",
    "api.anthropic.com",
}


def is_llm_host(host: str) -> bool:
    """True if `host` is a known LLM provider endpoint."""
    return host in LLM_HOSTS


def host_to_provider(host: str) -> str | None:
    """Map a well-known LLM host back to its provider name (for policy matching)."""
    if host == "api.openai.com":
        return "openai"
    if host == "api.anthropic.com":
        return "anthropic"
    return None
