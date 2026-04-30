"""Redis-primary policy storage with YAML bootstrap.

Source of truth for per-agent policies is Redis: `policy:{agent_id}` →
JSON-encoded `Policy`. YAML at `config/policies.yaml` is read once at
boot via `bootstrap_from_yaml()` and ONLY for entries not yet in Redis
(idempotent — never overwrites Redis values).

The proxy enforcer refetches per-request via `get_policy()` so that a
PUT through the orchestrator takes effect on the very next call without
a restart or cache-invalidation step. Cost is ~1 Redis GET per request.

Locked interface (do not change without spec update):

    get_policy(agent_id)        -> Policy | None
    put_policy(agent_id, p)     -> None
    list_policy_ids()           -> set[str]
    bootstrap_from_yaml()       -> int   (rows newly written)
"""
from __future__ import annotations

import json
import logging
import os
import pathlib

import redis.asyncio as redis
from pydantic import ValidationError

from services.proxy._redis import client as redis_client
from services.proxy.policy import Policy, load_policies_from_yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = pathlib.Path(os.environ.get("CONFIG_DIR", "/app/config"))


def _key(agent_id: str) -> str:
    return f"policy:{agent_id}"


async def get_policy(agent_id: str) -> Policy | None:
    """Return the Pydantic Policy for `agent_id`, or None if absent.

    Reads `policy:{agent_id}` from Redis first. If absent there, falls back
    to the YAML file (read each call — cheap; YAML is tiny). Does NOT auto-
    bootstrap on read — only the explicit `bootstrap_from_yaml()` call
    writes to Redis.

    Returns None on:
      * key missing in both Redis and YAML
      * malformed JSON in Redis (logged)
      * Pydantic validation failure (logged)

    Raises `redis.RedisError` on Redis transport failure — callers in the
    proxy enforcer translate to a fail-closed 503 deny.
    """
    r = await redis_client()
    raw = await r.get(_key(agent_id))
    if raw is not None:
        try:
            return Policy.model_validate_json(raw)
        except (ValidationError, ValueError) as e:
            logger.error(
                "policy_store: malformed policy JSON for %s: %s", agent_id, e,
            )
            return None

    # Redis miss → YAML fallback (read-only, no write).
    yaml_path = CONFIG_DIR / "policies.yaml"
    yaml_policies = load_policies_from_yaml(yaml_path)
    return yaml_policies.get(agent_id)


async def put_policy(agent_id: str, policy: Policy) -> None:
    """Write `policy` to Redis under `policy:{agent_id}` as JSON.

    Validates by re-instantiating Policy from the dump before writing —
    catches malformed updates that bypassed FastAPI body validation.
    Raises `pydantic.ValidationError` if revalidation fails;
    `redis.RedisError` if Redis is unreachable.
    """
    payload = policy.model_dump(mode="json")
    # Defensive re-validation: catch any drift between dump and schema.
    Policy.model_validate(payload)
    r = await redis_client()
    await r.set(_key(agent_id), json.dumps(payload))


async def list_policy_ids() -> set[str]:
    """Union of agent_ids present in Redis (`policy:*`) and in YAML."""
    ids: set[str] = set()
    r = await redis_client()
    try:
        async for key in r.scan_iter(match="policy:*"):
            # Strip "policy:" prefix.
            if key.startswith("policy:"):
                ids.add(key[len("policy:"):])
    except redis.RedisError as e:
        logger.error("policy_store: redis scan failed: %s", e)
        # Don't raise — caller may still want YAML names.

    yaml_path = CONFIG_DIR / "policies.yaml"
    try:
        ids.update(load_policies_from_yaml(yaml_path).keys())
    except Exception as e:  # noqa: BLE001 — best effort
        logger.warning("policy_store: yaml read failed: %s", e)
    return ids


async def bootstrap_from_yaml() -> int:
    """For each agent in YAML, write Redis `policy:{agent_id}` only if absent.

    Idempotent: never overwrites existing Redis values. Returns the number
    of new entries written. Safe to call multiple times. Caller decides
    when to invoke — typically once on orchestrator startup.
    """
    yaml_path = CONFIG_DIR / "policies.yaml"
    yaml_policies = load_policies_from_yaml(yaml_path)
    if not yaml_policies:
        return 0

    r = await redis_client()
    written = 0
    for agent_id, policy in yaml_policies.items():
        payload = json.dumps(policy.model_dump(mode="json"))
        # SETNX → only writes if key is absent. True == new write.
        if await r.setnx(_key(agent_id), payload):
            written += 1
    logger.info(
        "policy_store: bootstrap from %s — wrote %d new (of %d in yaml)",
        yaml_path, written, len(yaml_policies),
    )
    return written
