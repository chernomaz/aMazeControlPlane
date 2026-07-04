"""S8 — Admin user management API: create / list / delete users.

Every endpoint is admin-only (`auth.current_user_admin` → 403 for non-admins).
Identity logic lives in `services.orchestrator.auth`; this module is the thin
HTTP transport, mirroring `auth_routes.py`. Agent (re)assignment lives in
`routers/agents.py` next to the agent helpers — kept there so the agent
namespace stays in one place.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from services.orchestrator import auth
from services.orchestrator.auth import User

logger = logging.getLogger(__name__)

router = APIRouter()


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "user"          # "admin" | "user" — validated in auth.create_user


def _payload(u: User) -> dict:
    return {"user_id": u.user_id, "username": u.username, "role": u.role}


@router.get("/auth/users")
async def list_users(
    request: Request, admin: User = Depends(auth.current_user_admin)
) -> list[dict]:
    """All users. Admin-only."""
    users = await auth.list_users(request.app.state.redis)
    return [_payload(u) for u in users]


@router.post("/auth/users", status_code=201)
async def create_user(
    body: NewUser, request: Request, admin: User = Depends(auth.current_user_admin)
) -> dict:
    """Create a user with an explicit role. Admin-only. 409 if name taken,
    422 on invalid role / empty username / over-long password."""
    user = await auth.create_user(
        request.app.state.redis, body.username, body.password, role=body.role
    )
    logger.info("users: %s created user=%s role=%s", admin.username,
                user.username, user.role)
    return _payload(user)


@router.delete("/auth/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str, request: Request, admin: User = Depends(auth.current_user_admin)
) -> None:
    """Delete a user (orphans their agents). Admin-only. An admin cannot delete
    their own account."""
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="cannot-delete-self")
    await auth.delete_user(request.app.state.redis, user_id)
    return None
