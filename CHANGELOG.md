# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7] - 2026-06-29

Multi-user access control: real session auth, per-user agent ownership, and an
admin user-management console.

### Added

- **Session authentication.** Cookie-based login/logout (`POST /auth/login`,
  `/auth/logout`, `GET /auth/me`) with bcrypt-hashed credentials and opaque,
  TTL-refreshed session tokens in Redis. All control-plane routes are now
  gated; the SDK/proxy agent-bearer path is untouched — human identity and
  agent identity stay separate seams. `auth.py` is the single OIDC-swap point.
- **Per-user agent ownership.** Every agent has exactly one owner. A user
  *claims* an agent id before it registers; on self-registration ownership
  binds to the claimer automatically. Unclaimed registrations are quarantined
  (owner-less) until an admin adopts them. Policy, debugger, messaging, stats,
  and audit routes are owner-gated (admins bypass).
- **Admin user-management console (Users tab).** Admins create accounts with a
  role (`admin` or regular), list and delete them, and assign or reassign any
  agent to a single owner — `GET/POST /auth/users`, `DELETE /auth/users/{id}`,
  `POST /agents/{id}/assign`. Regular users see only the agents they own;
  admins see all.
- **Default bootstrap admin** `admin` / `admin`, configurable via
  `AMAZE_ADMIN_USER` / `AMAZE_ADMIN_PASSWORD`.
- Full-stack system tests: `tests/test_s7_auth.py`, `test_s7_ownership.py`,
  `test_s7_debug_authz.py`, and `test_s8_users.py` (admin-only user CRUD,
  single-owner reassignment, agent-orphaned-on-delete) — real stack, no mocks.

### Fixed

- **Stale agent list after switching users.** The dashboard kept the previous
  session's React Query cache across login/logout, so a newly logged-in user
  saw the prior user's agents until a manual browser refresh. Login and logout
  now clear the query cache (and prime `me` from the login response).

[0.7]: https://github.com/chernomaz/aMazeControlPlane/releases/tag/v0.7

## [0.6.0] - 2026-06-27

First tagged release. Headlines the multi-user live debugger.

### Added

- **Multi-user live debugger.** Multiple users can step through the **same**
  agent concurrently and independently — each browser sees only its own paused
  steps, advances only its own queue, and never affects anyone else's session.
  A per-browser debug-user id propagates from the UI through the agent runtime
  onto every outbound call, so the proxy parks each intercepted step under that
  user's namespace (`debug:{agent}:{user}:*`). Untagged traffic is never parked.
- Full-stack isolation tests (`tests/test_s6_debug_multiuser.py`, ST-MU.1–5):
  independent queues, independent Next, per-user dead-man's-switch expiry, A2A
  peer attribution, and untagged-bypass — run against the real stack, no mocks.

### Fixed

- **UI `apiFetch` dropped `Content-Type` when a caller passed custom headers**,
  so JSON requests (enable-debug `PUT`, send-message `POST`) went out as
  `text/plain` and were rejected with `422`. Header merge order corrected, so
  every caller keeps `application/json`.
- `index.html` is now served with `Cache-Control: no-cache` (hashed assets stay
  `immutable`), so UI redeploys are picked up without a manual hard refresh.

### Changed

- demo-mcp persists its MySQL data across restarts (named volume + idempotent
  init/seed guard) — the database is initialized and seeded only on first boot;
  later restarts come up fast.

[0.6.0]: https://github.com/chernomaz/aMazeControlPlane/releases/tag/v0.6.0
</content>
