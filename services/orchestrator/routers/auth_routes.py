"""S7-T1.3 — Session auth HTTP API: login / logout / me / signup.

Thin transport layer over `services.orchestrator.auth`. All identity logic
(hashing, session minting, cookie resolution) lives in `auth`; this module
only marshals HTTP requests/responses and sets the session cookie. By calling
through the `auth` module namespace (`auth.current_user`, `auth.SESSION_COOKIE`,
...), the OIDC swap seam stays confined to `auth.py`.

Redis is read per-handler from `request.app.state.redis`.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from services.orchestrator import auth
from services.orchestrator.auth import User

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Request bodies ----------------------------------------------------------

class Credentials(BaseModel):
    username: str
    password: str


# --- Helpers -----------------------------------------------------------------

def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _cookie_secure() -> bool:
    """Whether to mark the session cookie `Secure`. Off by default so the
    localhost demo works without TLS; flip `COOKIE_SECURE=1` behind HTTPS."""
    return _truthy(os.environ.get("COOKIE_SECURE"))


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        max_age=auth.USER_SESSION_TTL,
        path="/",
    )


def _user_payload(user: User) -> dict:
    return {"user_id": user.user_id, "username": user.username, "role": user.role}


# --- Endpoints ---------------------------------------------------------------

@router.post("/auth/login")
async def login(body: Credentials, request: Request, response: Response) -> dict:
    """Authenticate credentials, mint a session, set the cookie."""
    r = request.app.state.redis
    user = await auth.authenticate(r, body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid-credentials")
    token = await auth.issue_session(r, user.user_id)
    _set_session_cookie(response, token)
    logger.info("auth: login username=%s", user.username)
    return _user_payload(user)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    """Revoke the current session and clear the cookie."""
    r = request.app.state.redis
    token = request.cookies.get(auth.SESSION_COOKIE)
    await auth.revoke_session(r, token)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return None


@router.get("/auth/me")
async def me(user: User = Depends(auth.current_user)) -> User:
    """Who-am-I: resolve the session cookie → the authenticated principal."""
    return user


@router.post("/auth/signup")
async def signup(body: Credentials, request: Request, response: Response) -> dict:
    """Self-service registration, gated by `ALLOW_SIGNUP`. Creates a plain
    `user`-role account, then logs in by issuing a session immediately."""
    if not _truthy(os.environ.get("ALLOW_SIGNUP", "false")):
        raise HTTPException(status_code=403, detail="signup-disabled")
    r = request.app.state.redis
    user = await auth.create_user(r, body.username, body.password, role="user")
    token = await auth.issue_session(r, user.user_id)
    _set_session_cookie(response, token)
    logger.info("auth: signup username=%s", user.username)
    return _user_payload(user)
