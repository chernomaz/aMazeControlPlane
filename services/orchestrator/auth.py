"""S7 — User management: session auth + per-user agent ownership.

This module is the SINGLE seam for human identity. A future OIDC/JWT swap
changes only the body of `current_user()` / `issue_session()` here; every
protected route keeps using `Depends(current_user)` unchanged.

It is deliberately separate from AGENT identity. Agent identity lives in the
proxy (`X-Amaze-Bearer` → agent_id, on the enforcement hot path) and is never
touched here. Human identity is a session cookie, validated only at the
orchestrator control-plane API. Two principals, two components. No file under
`services/proxy/` imports or is imported by this module.

Redis keyspace (all on the orchestrator's `app.state.redis`, decode_responses=True):

  user:{user_id}            HASH   {username, created_at, role}   role ∈ {admin,user}
  username:{username}       STRING → user_id                      (uniqueness + login)
  user:{user_id}:cred       STRING → bcrypt hash                  (never returned by a read path)
  user_session:{token}      STRING → user_id                      (TTL, sliding refresh)
  agent:{agent_id}:owner    STRING → user_id                      (no TTL; permanent)
  user:{user_id}:agents     SET    {agent_id, ...}                (reverse index)
  agent:{agent_id}:claim    STRING → user_id                      (NX pre-registration reserve)

Ownership is a *sidecar* of the existing `agent:{id}:*` namespace — never a
prefix on it — so a future workspace layer slots in without a data migration.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid

import bcrypt
from fastapi import HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

SESSION_COOKIE = "amaze_session"
USER_SESSION_TTL = 7 * 24 * 60 * 60        # 7 days
BCRYPT_MAX_BYTES = 72                       # bcrypt silently truncates beyond this


# --- Model -------------------------------------------------------------------

class User(BaseModel):
    """The authenticated principal. Returned by `current_user`; never carries
    the password hash."""
    user_id: str
    username: str
    role: str = "user"                      # "admin" | "user"


# --- Password hashing (bcrypt) ----------------------------------------------

def hash_password(password: str) -> str:
    """bcrypt hash. Rejects > 72 bytes rather than silently truncating."""
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise HTTPException(status_code=422, detail="password-too-long")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    raw = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except ValueError:
        # Malformed stored hash — fail closed.
        return False


# --- User CRUD ---------------------------------------------------------------

async def create_user(r, username: str, password: str, role: str = "user") -> User:
    """Create a user. Raises 409 if the username is taken.

    `username:{name}` is set with NX to make creation atomic against races.
    """
    username = username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="username-required")
    if role not in ("admin", "user"):
        raise HTTPException(status_code=422, detail="invalid-role")

    user_id = uuid.uuid4().hex
    # Reserve the username atomically; loser of a race gets 409.
    if not await r.set(f"username:{username}", user_id, nx=True):
        raise HTTPException(status_code=409, detail="username-taken")

    cred = hash_password(password)
    await r.hset(
        f"user:{user_id}",
        mapping={"username": username, "created_at": int(time.time()), "role": role},
    )
    await r.set(f"user:{user_id}:cred", cred)
    logger.info("auth: created user username=%s role=%s", username, role)
    return User(user_id=user_id, username=username, role=role)


async def list_users(r) -> list[User]:
    """All users (admin console). Scans `user:*` and skips the `:cred` /
    `:agents` sidecar keys — only the `user:{id}` hash is a real user."""
    users: list[User] = []
    async for key in r.scan_iter(match="user:*"):
        rest = key[len("user:"):]
        if ":" in rest:                         # user:{id}:cred / :agents
            continue
        data = await r.hgetall(key)
        if not data:
            continue
        users.append(User(user_id=rest, username=data.get("username", ""),
                          role=data.get("role", "user")))
    return users


async def delete_user(r, user_id: str) -> None:
    """Delete a user. Orphans any agents they owned (drops the owner key so an
    admin can reassign later) and revokes all of the user's sessions. 404 if
    the user does not exist."""
    data = await r.hgetall(f"user:{user_id}")
    if not data:
        raise HTTPException(status_code=404, detail="user-not-found")
    username = data.get("username")
    owned = await r.smembers(f"user:{user_id}:agents")
    for agent_id in owned:
        # Orphan, don't delete: the agent stays registered, just unowned.
        await r.delete(f"agent:{agent_id}:owner")
    keys = [f"user:{user_id}", f"user:{user_id}:cred", f"user:{user_id}:agents"]
    if username:
        keys.append(f"username:{username}")
    await r.delete(*keys)
    async for key in r.scan_iter(match="user_session:*"):
        if await r.get(key) == user_id:
            await r.delete(key)
    logger.info("auth: deleted user user_id=%s username=%s orphaned_agents=%d",
                user_id, username, len(owned))


async def authenticate(r, username: str, password: str) -> User | None:
    """Return the User on valid credentials, else None.

    Performs a hash comparison even when the user is absent (dummy hash) to
    keep timing roughly uniform and avoid username enumeration.
    """
    username = (username or "").strip()
    user_id = await r.get(f"username:{username}")
    cred = await r.get(f"user:{user_id}:cred") if user_id else None
    if cred is None:
        # Constant-ish work for the unknown-user path.
        verify_password(password, "$2b$12$" + "x" * 53)
        return None
    if not verify_password(password, cred):
        return None
    data = await r.hgetall(f"user:{user_id}")
    return User(user_id=user_id, username=data.get("username", username),
                role=data.get("role", "user"))


# --- Sessions ----------------------------------------------------------------

async def issue_session(r, user_id: str) -> str:
    """Mint an opaque session token → `user_session:{token}` with TTL."""
    token = secrets.token_urlsafe(32)
    await r.set(f"user_session:{token}", user_id, ex=USER_SESSION_TTL)
    return token


async def revoke_session(r, token: str) -> None:
    if token:
        await r.delete(f"user_session:{token}")


# --- Dependencies ------------------------------------------------------------

async def current_user(request: Request) -> User:
    """FastAPI dependency: resolve the session cookie → User, or 401.

    THE OIDC swap seam — only this body changes for a future IdP. Slides the
    session TTL on every authenticated request (keep-alive).
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="not-authenticated")
    r = request.app.state.redis
    user_id = await r.get(f"user_session:{token}")
    if not user_id:
        raise HTTPException(status_code=401, detail="not-authenticated")
    data = await r.hgetall(f"user:{user_id}")
    if not data:
        # Session points at a deleted user — revoke and reject.
        await r.delete(f"user_session:{token}")
        raise HTTPException(status_code=401, detail="not-authenticated")
    await r.expire(f"user_session:{token}", USER_SESSION_TTL)
    return User(user_id=user_id, username=data.get("username", ""),
                role=data.get("role", "user"))


async def current_user_admin(request: Request) -> User:
    """Like `current_user`, but 403 unless role == admin."""
    user = await current_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin-required")
    return user


# --- Ownership ---------------------------------------------------------------

async def require_agent_owner(r, agent_id: str, user: User) -> None:
    """Authorize `user` to act on `agent_id`. 404 if unowned/unknown (no
    enumeration), 403 if owned by someone else. Admins bypass."""
    owner = await r.get(f"agent:{agent_id}:owner")
    if owner is None:
        # Unowned: either a never-existed id or a registered orphan (quarantined,
        # awaiting adoption). An admin may act on a *registered* orphan (e.g.
        # inspect/debug it before adopting in S8); everyone else gets 404 so the
        # orphan stays hidden and ids aren't enumerable.
        if user.role == "admin" and await r.exists(f"agent:{agent_id}:endpoint"):
            return
        raise HTTPException(status_code=404, detail="agent-not-found")
    if owner != user.user_id and user.role != "admin":
        raise HTTPException(status_code=403, detail="not-agent-owner")


async def bind_owner(r, agent_id: str, user_id: str) -> None:
    """Assign ownership: set the sidecar owner key + reverse index. Idempotent."""
    await r.set(f"agent:{agent_id}:owner", user_id)
    await r.sadd(f"user:{user_id}:agents", agent_id)


async def reassign_owner(r, agent_id: str, new_user_id: str) -> None:
    """Move an agent to a new owner: detach from the previous owner's reverse
    index, then bind to the new one. Enforces single-owner. Idempotent."""
    old = await r.get(f"agent:{agent_id}:owner")
    if old and old != new_user_id:
        await r.srem(f"user:{old}:agents", agent_id)
    await bind_owner(r, agent_id, new_user_id)


async def list_owned_agents(r, user_id: str) -> set[str]:
    """Agent ids owned by `user_id` (the reverse index)."""
    return set(await r.smembers(f"user:{user_id}:agents"))
