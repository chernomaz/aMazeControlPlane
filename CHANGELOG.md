# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8] - 2026-07-11

Per-tool PII redaction for MCP tool calls: a new proxy addon that redacts
configurable entity types out of tool inputs and outputs — powered by
Microsoft Presidio + spaCy NER (`en_core_web_lg`), with a regex-only
fallback for low-footprint deployments.

### Added

- **PII redaction addon.** New `PiiRedactor` proxy addon between
  `stream_blocker` and `counters`. Redacts per-parameter PII on MCP
  `tools/call` request bodies before they leave the proxy; redacts result
  bodies (both buffered JSON and streamed SSE — POST-inline and GET-async)
  before they reach the agent. Covered entities: `EMAIL_ADDRESS`,
  `CREDIT_CARD`, `PHONE_NUMBER`, `US_SSN`, `IP_ADDRESS`, `PERSON`, `LOCATION`,
  `URL`, `IBAN_CODE`. Presidio errors do not deny — the affected field is
  replaced with `<PII_REDACTION_ERROR>` and the tool call proceeds.
- **spaCy NER backend** (default). `PII_NLP_MODE=ner` uses Presidio's
  `AnalyzerEngine` backed by spaCy `en_core_web_lg`. Reliably catches
  single-token proper nouns (Nashville, Portland, Boston) and disambiguates
  PERSON vs LOCATION by context. Custom recognizers stay for US_SSN and
  structured entities. Image grows ~1.5 GB to bake in the model.
  `PII_NLP_MODE=regex` keeps the small-image, regex-only path for
  low-footprint deployments.
- **JSON-in-string parsing.** When a string leaf is itself a serialized JSON
  document (e.g. the `text` field of an MCP tool result whose tool returns
  pure JSON), `redact_json_text_fields` parses it, walks the parsed
  structure, and re-serializes so the analyzer sees bare values —
  `"Grace Hall"` not `"{\"name\":\"Grace Hall\"}"`. Per-leaf analyzer calls
  give spaCy the isolation it needs for reliable NER; trades ~2-3 s on 30-row
  responses for consistent name-recall accuracy.
- **Policy schema.** `Policy` gains a `pii_config` sub-object of shape
  `{enabled, tools: {tool_name: {input: {param: {entities}}, output: {entities}}}}`.
  Absent or `null` = no redaction (fully backwards compatible with older
  policies). Unknown entity labels are rejected at model-validation time.
- **Orchestrator endpoints.** `GET /policy/{agent_id}/pii`,
  `PUT /policy/{agent_id}/pii` (surgical sub-object write; other Policy fields
  preserved), and `POST /policy/{agent_id}/pii/preview` (stateless dry-run
  that feeds the UI preview panel). All owner-gated via `require_agent_owner`.
- **PII Redaction UI tab.** New `AgentPiiRedaction` page slotted between
  Policy and Debugger. Lists tools this agent is authorized to use (union of
  `allowed_tools` and graph tool steps), pulls parameter names + types from
  the MCP server's `inputSchema`, and exposes a pill-picker per parameter and
  per response-body entity list. Non-string parameters are visibly disabled.
  A live "Redaction preview" panel calls the `/preview` endpoint. Sticky
  Save/Reset footer with dirty-state tracking.
- **Trace log.** Every audit record now carries a `pii_redacted` field, and
  the trace-detail table shows a small "PII redacted" chip next to tool
  spans where it's true.
- **AuditLog coordination.** `AuditLog` gains a `responseheaders`
  short-circuit on `flow.metadata["amaze_pii_owned_stream"]` — when
  PiiRedactor takes over the response-side stream (POST inline SSE with an
  output rule) AuditLog leaves `flow.response.stream` untouched and
  PiiRedactor writes the audit record directly. A new in-process cache
  `_pii_entities_cache`, mirrored from the pending Redis payload, lets the
  GET-SSE stream handler redact frames synchronously without a blocking
  Redis GET on the event-loop thread. Cache has a TTL sweep bound to
  `MCP_PENDING_TTL` (120 s) to prevent unbounded growth on non-happy
  termination paths.
- **Eager spaCy preload** at proxy startup — first request no longer blocks
  ~3-5 s on model init.
- **Recursion depth cap** (`_MAX_JSON_DEPTH = 32`) and **64 KB parse cap**
  on the JSON walk — bounds stack size and worst-case parse cost against
  hostile / misbehaving MCP servers.
- **Nested-argument walking.** Input redaction recurses into dict/list-
  shaped argument values via `redact_json_text_fields`, not just top-level
  string params. A payload like `{contact: {email: "..."}}` is now covered.
- **Canonical `safe_redact_json` helper.** Single shape-preserving wrapper
  used by both `audit_log` and `pii_redactor` — same failure semantic in
  both places (return input unchanged on error, log at WARNING). Preserves
  dict/list shape so callers doing `json.dumps(that)` don't get their tool
  response silently rewritten to a bare string on a Presidio exception.
- **Honest `pii_redacted` audit field.** Set to `"true"` only when an actual
  span was replaced, not merely when a rule was configured. Aligns the
  trace-detail "PII redacted" chip with what it says.
- **Pinned SSE frame separator** on first sighting in both PiiRedactor's
  POST-SSE handler and AuditLog's GET-SSE handler — long-lived streams that
  switch between LF and CRLF mid-stream no longer glue frames together.
- **Demo enhancements.** `sql_query` tool now returns pure JSON (dropped
  the `"N row(s):\n"` prefix); demo `users` table gains `phone` (US area
  codes in 5 realistic formats) and Luhn-valid `credit_card` columns. Two
  more entities to exercise the redactor end-to-end.

### Dependencies

- Added `presidio-analyzer>=2.2,<3` and `spacy>=3.8,<4`.
  `presidio-anonymizer` was deliberately NOT added — its `cryptography>=46`
  constraint conflicts with `mitmproxy 11`'s `cryptography<44.1`. Text
  replacement is done inline in `pii_engine`, which also gives us direct
  control over the `<ENTITY_LABEL>` placeholder format.
- Docker image adds `python -m spacy download en_core_web_lg`
  (~600 MB model). Overridable via `PII_SPACY_MODEL` env var.
- `docker-compose.yml` sets `PII_NLP_MODE: ner` on the `amaze` service.

### Notes

- LLM chat bodies and A2A traffic are out of scope for this release; the
  redactor bypasses non-MCP flows.
- Presidio's built-in `UsSsnRecognizer` needs surrounding-word context;
  in NER mode it is removed from the registry and replaced by our custom
  standalone SSN regex.

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
