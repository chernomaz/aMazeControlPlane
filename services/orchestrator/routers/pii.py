"""GET/PUT/preview endpoints for the per-agent PII redaction config.

The `pii_config` sub-object lives INSIDE the agent's Policy in Redis. The PUT
here is surgical — it reads the current policy, merges only the `pii_config`
field, and writes back. This mirrors the last-writer-wins semantics of
`PUT /policy/{id}` (there is no cross-tab locking today; when we add it, we
add it repo-wide).

The preview endpoint runs redaction against a caller-supplied sample payload
WITHOUT persisting anything. Used by the UI's "Redaction preview" panel to
show what the current selections would produce.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.orchestrator.auth import User, current_user, require_agent_owner
from services.proxy import policy_store
from services.proxy.pii_engine import (
    redact,
    redact_json_text_fields,
)
from services.proxy.policy import PII_ENTITY_LABELS, PiiConfig, Policy

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /policy/{agent_id}/pii
# ---------------------------------------------------------------------------

@router.get("/policy/{agent_id}/pii")
async def get_pii_config(
    agent_id: str,
    request: Request,
    user: User = Depends(current_user),
) -> dict:
    """Return the pii_config sub-object of the agent's policy. Returns an
    empty enabled config when the policy exists but has no pii_config yet.
    404 when the policy itself is absent.
    """
    await require_agent_owner(request.app.state.redis, agent_id, user)
    try:
        policy = await policy_store.get_policy(agent_id)
    except redis.RedisError as e:
        logger.error("pii GET: redis unreachable for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    if policy is None:
        raise HTTPException(status_code=404, detail="policy-not-found")
    if policy.pii_config is None:
        return PiiConfig().model_dump(mode="json")
    return policy.pii_config.model_dump(mode="json")


# ---------------------------------------------------------------------------
# PUT /policy/{agent_id}/pii
# ---------------------------------------------------------------------------

@router.put("/policy/{agent_id}/pii")
async def put_pii_config(
    agent_id: str,
    pii_config: PiiConfig,
    request: Request,
    user: User = Depends(current_user),
) -> dict:
    """Persist ONLY the pii_config sub-object. Reads the current policy from
    Redis, replaces its pii_config, and writes the whole policy back. If the
    policy is absent this returns 404 — the PII tab is only reachable after a
    policy exists.

    FastAPI validates the body against `PiiConfig`, which in turn validates
    each entity against `PII_ENTITY_LABELS` — invalid entity → 422.
    """
    await require_agent_owner(request.app.state.redis, agent_id, user)
    try:
        policy = await policy_store.get_policy(agent_id)
    except redis.RedisError as e:
        logger.error("pii PUT: redis unreachable for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e
    if policy is None:
        raise HTTPException(status_code=404, detail="policy-not-found")

    policy = policy.model_copy(update={"pii_config": pii_config})
    try:
        await policy_store.put_policy(agent_id, policy)
    except redis.RedisError as e:
        logger.error("pii PUT: redis write failed for %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail="redis-unavailable") from e

    tools_count = len(pii_config.tools)
    logger.info("pii PUT: agent_id=%s tools=%d enabled=%s",
                agent_id, tools_count, pii_config.enabled)
    return {"updated": True, "agent_id": agent_id, "tools": tools_count}


# ---------------------------------------------------------------------------
# POST /policy/{agent_id}/pii/preview  — dry-run
# ---------------------------------------------------------------------------

class PiiPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(..., description="Tool name; only used for context/logging.")
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="Sample tool arguments to apply the input rules to.",
    )
    input_entities: dict[str, list[str]] = Field(
        default_factory=dict,
        description="{param_name: [entities]} — mirrors ToolPiiConfig.input.",
    )
    response_text: str | None = Field(
        default=None,
        description="Sample response text to apply the output rule to.",
    )
    output_entities: list[str] = Field(
        default_factory=list,
        description="Entity list for the response.",
    )


class PiiPreviewResponse(BaseModel):
    input: dict[str, Any]
    response_text: str | None


def _validate_entities(entities: list[str], where: str) -> None:
    for e in entities:
        if e not in PII_ENTITY_LABELS:
            raise HTTPException(
                status_code=422,
                detail=f"invalid entity {e!r} in {where}: must be one of {sorted(PII_ENTITY_LABELS)}",
            )


@router.post("/policy/{agent_id}/pii/preview")
async def preview_pii(
    agent_id: str,
    body: PiiPreviewRequest,
    request: Request,
    user: User = Depends(current_user),
) -> PiiPreviewResponse:
    """Run redaction against sample payloads with UNSAVED rules. Nothing is
    persisted — the endpoint is stateless with respect to Redis, only used
    for the UI preview panel."""
    await require_agent_owner(request.app.state.redis, agent_id, user)

    _validate_entities(body.output_entities, "output_entities")
    for param, ents in body.input_entities.items():
        _validate_entities(ents, f"input_entities[{param}]")

    # Input redaction — walk each configured param.
    redacted_input: dict[str, Any] = {}
    for param, value in body.input.items():
        ents = body.input_entities.get(param) or []
        if isinstance(value, str) and ents:
            redacted_input[param] = redact(value, ents)
        else:
            redacted_input[param] = value

    # Output redaction — mirrors what the addon does on tool responses:
    # walks JSON structures, and if the response_text itself is JSON, feeds
    # spaCy only bare leaf values instead of the wrapping syntax.
    redacted_output: str | None = None
    if body.response_text is not None:
        if body.output_entities:
            result = redact_json_text_fields(body.response_text, body.output_entities)
            redacted_output = result if isinstance(result, str) else str(result)
        else:
            redacted_output = body.response_text

    return PiiPreviewResponse(input=redacted_input, response_text=redacted_output)
