"""
store.py — in-memory policy store loaded from YAML.

Sprint 2: loads A2A policies from `policies/agents.yaml`.
Future sprints extend with MCP policies and limits sections.
"""
import os
from typing import Optional

import yaml

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "policies", "agents.yaml")


class PolicyStore:
    def __init__(self, path: Optional[str] = None):
        self._path = path or DEFAULT_PATH
        self._agents: dict = {}
        self.reload()

    def reload(self) -> None:
        with open(self._path, "r") as f:
            data = yaml.safe_load(f) or {}
        self._agents = data.get("agents", {}) or {}

    def get_agent(self, agent_id: str) -> Optional[dict]:
        return self._agents.get(agent_id)

    def has_agent(self, agent_id: str) -> bool:
        return agent_id in self._agents


_store: Optional[PolicyStore] = None


def get_store() -> PolicyStore:
    global _store
    if _store is None:
        _store = PolicyStore()
    return _store
