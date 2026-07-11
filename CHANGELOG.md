# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.1] - 2026-07-11

Hardened the v0.8 PII redactor: upgraded the NER backend from regex-only to
Presidio + spaCy `en_core_web_lg`, redesigned the JSON walk to feed spaCy
bare values, and landed a code-review pass fixing 16 items across
correctness, security, and maintainability.

### Added

- **spaCy NER backend.** `PII_NLP_MODE=ner` (now the default in
  `docker-compose.yml`) swaps the regex-only PERSON/LOCATION path for
  Presidio's `AnalyzerEngine` backed by spaCy `en_core_web_lg`. Reliably
  catches single-token proper nouns (Nashville, Portland, Boston) and
  disambiguates PERSON vs LOCATION by context. Custom recognizers stay for
  US_SSN and structured entities. Image grows ~1.5 GB to bake in the model.
- **JSON-in-string parsing.** When a string leaf is itself a serialized JSON
  document (e.g. the `text` field of an MCP tool result whose tool returns
  pure JSON), `redact_json_text_fields` parses it, walks the parsed
  structure, and re-serializes so the analyzer sees bare values —
  `"Grace Hall"` not `"{\"name\":\"Grace Hall\"}"`. Fixes ~10% recall gap
  spaCy had on JSON-shaped payloads.
- **Per-leaf analyzer calls.** Each string leaf gets its own analyzer call,
  giving spaCy the isolation it needs for reliable NER. Trades off some
  latency (~2-3 s on 30-row responses) for the accuracy jump — earlier
  batched approach missed ~10-15% of names when leaves were concatenated
  with separators.
- **Demo data enhancements.** `sql_query` tool now returns pure JSON
  (dropped the `"N row(s):\n"` prefix); demo `users` table gains `phone`
  (US area codes in 5 realistic formats) and Luhn-valid `credit_card`
  columns. Two more entities to exercise the redactor end-to-end.
- **Cache TTL + sweep** on the in-process `_pii_entities_cache` — was
  unbounded on non-happy termination paths, now sweeps entries older than
  `MCP_PENDING_TTL` (120 s) on every insert.
- **Eager spaCy preload** at proxy startup — first request no longer blocks
  ~3-5 s on model init.
- **Recursion depth cap** (`_MAX_JSON_DEPTH = 32`) on the JSON walk to
  bound stack size against hostile / misbehaving MCP servers.
- **64 KB size cap** on `_try_parse_json_container` — a 10 MB SQL leaf that
  happens to start with `{` no longer pays a full parse.
- **Two new unit tests** for the cache TTL sweep and expiry paths.

### Changed

- **Canonical `safe_redact_json`.** Merged the two module-local helpers
  (`audit_log._safe_redact_json` returned input on error;
  `pii_redactor._safe_redact_json` returned a bare string). Both callers
  now use the same shape-preserving helper from `pii_engine`.
- **`pii_redacted` audit field now reflects reality.** Was set to `"true"`
  whenever a rule ran; now `"true"` only when actual span replacement
  happened. Aligns the trace-detail "PII redacted" chip with what it says.
- **Single audit record per POST-SSE span** documented as intentional; extra
  JSON-RPC result frames in one POST body are logged at WARNING rather than
  silently accumulating audit records.
- **Pinned SSE frame separator** on first sighting — long-lived streams that
  switch between LF and CRLF mid-stream no longer glue frames together.
- **Compact JSON re-serialization** (`separators=(",", ":")`) — preserves
  wire size when rewriting the body; the caller's exact whitespace and key
  ordering is still lost, documented as tradeoff.
- **Nested-argument walking.** Input redaction now recurses into
  dict/list-shaped argument values, not just top-level strings. A payload
  like `{contact: {email: "..."}}` previously passed through.
- **Presidio built-in SSN recognizer removed** in NER mode — was firing
  alongside our custom SSN pattern and doing duplicate work.
- **Docstring drift fixed** for `amaze_pii_owned_stream` (previously
  referenced a dead `amaze_pii_owned_audit` flag).

### Fixed

- **Shape-preserving errors.** `safe_redact_json` in `pii_redactor.py`
  returned a bare `"<PII_REDACTION_ERROR>"` string on any Presidio
  exception. Callers assumed dict/list shape was preserved and did
  `json.dumps(that)` — the tool response would silently rewrite from
  `{"result":...}` to `"\"<PII_REDACTION_ERROR>\""` and agents parsing by
  shape would break.
- **Audit records for PII-free responses.** POST-SSE handler only wrote an
  audit record when a frame was actually redacted, so a MCP call that
  returned no PII in a policy that had PII rules configured left no audit
  trail. Every JSON-RPC result frame now produces exactly one audit
  record; `pii_redacted` reflects whether replacement happened.

### Dependencies

- `spacy>=3.8,<4` pinned explicitly (was transitive of `presidio-analyzer`).
- Docker image adds `python -m spacy download en_core_web_lg`
  (~600 MB model). Overridable via `PII_SPACY_MODEL` env var.
- `docker-compose.yml` sets `PII_NLP_MODE: ner` on the `amaze` service.

## [0.8] - 2026-07-10

Per-tool PII redaction for MCP tool calls: a new proxy addon that redacts
configurable entity types out of tool inputs and outputs — powered by
Microsoft Presidio's regex recognizers.

### Added

- **PII redaction addon.** New `PiiRedactor` proxy addon between
  `stream_blocker` and `counters`. Redacts per-parameter PII on MCP
  `tools/call` request bodies before they leave the proxy; redacts result
  bodies (both buffered JSON and streamed SSE — POST-inline and GET-async)
  before they reach the agent. Covered entities: `EMAIL_ADDRESS`,
  `CREDIT_CARD`, `PHONE_NUMBER`, `US_SSN`, `IP_ADDRESS`, `PERSON`, `LOCATION`,
  `URL`, `IBAN_CODE`. Presidio errors do not deny — the affected field is
  replaced with `<PII_REDACTION_ERROR>` and the tool call proceeds.
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

### Changed

- `AuditLog` gains a `responseheaders` short-circuit on
  `flow.metadata["amaze_pii_owned_stream"]` — when PiiRedactor takes over
  the response-side stream (POST inline SSE with an output rule) AuditLog
  leaves `flow.response.stream` untouched and PiiRedactor writes the audit
  record directly. A new in-process cache `_pii_entities_cache`, mirrored
  from the pending Redis payload, lets the GET-SSE stream handler redact
  frames synchronously without a blocking Redis GET on the event-loop thread.

### Dependencies

- Added `presidio-analyzer>=2.2,<3`. `presidio-anonymizer` was deliberately
  NOT added — its `cryptography>=46` constraint conflicts with
  `mitmproxy 11`'s `cryptography<44.1`. Text replacement is done inline in
  `pii_engine.redact`, which also gives us direct control over the
  `<ENTITY_LABEL>` placeholder format.

### Notes

- PERSON / LOCATION are covered by lightweight custom regex heuristics as an
  MVP placeholder. A drop-in swap to Presidio + spaCy NER is scaffolded
  behind a `PII_NLP_MODE` env flag for a future sprint.
- LLM chat bodies and A2A traffic are out of scope for this release; the
  redactor bypasses non-MCP flows.

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
