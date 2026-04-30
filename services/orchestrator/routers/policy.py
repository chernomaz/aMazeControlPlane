"""GET /policy/{agent_id} and PUT /policy/{agent_id} — Redis-backed policy CRUD.

S4-T2.2: Redis is the source of truth. YAML at `config/policies.yaml` is
read once at boot via `policy_store.bootstrap_from_yaml()` (wired in
main.py's lifespan) and ONLY for entries not yet present in Redis.

After PUT returns 200, the very next proxy request from that agent will
see the new policy — the proxy enforcer refetches per-request, so no
cache invalidation step is needed.
"""
from __future__ import annotations

import logging

import redis.asyncio as redis
from fastapi import APIRouter, HTTPException

from services.proxy import policy_store
from services.proxy.policy import Policy

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/policy/{agent_id}")
async def get_policy_endpoint(agent_id: str) -> dict:
    """Return the full policy JSON for `agent_id`. 404 if absent in Redis
    AND in the YAML fallback.
    """
    try:
        policy = await policy_store.get_policy(agent_id)
    except redis.RedisError as e:
        logger.error("policy GET: redis unreachable for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    if policy is None:
        raise HTTPException(status_code=404, detail="policy-not-found")
    return policy.model_dump(mode="json")


@router.put("/policy/{agent_id}")
async def put_policy_endpoint(agent_id: str, policy: Policy) -> dict:
    """Persist the full policy in Redis.

    FastAPI validates the body against the `Policy` Pydantic model — a
    malformed body returns 422 automatically. The `name` field is taken
    from the body; we do NOT enforce it equals `agent_id` (the YAML
    invariant) because the path is the canonical key here.
    """
    try:
        await policy_store.put_policy(agent_id, policy)
    except redis.RedisError as e:
        logger.error("policy PUT: redis unreachable for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    logger.info("policy PUT: agent_id=%s name=%s mode=%s",
                agent_id, policy.name, policy.mode)
    return {"updated": True, "agent_id": agent_id}
