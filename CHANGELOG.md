# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
