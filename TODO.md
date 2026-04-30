# TODO — open issues from Sprint 1/2/4 integration + code-review

Last updated: 2026-04-30.

## S4 sprint-close code review — should-fix items deferred for next sprint

Severity 🟡 findings from the 2026-04-30 review of S4. Blocking #1-4 fixed
in-sprint; the items below are tracked here. File:line references point at
the state of the codebase at S4 close.

### S4-CR-5. Path-param Redis key composition without validation
`services/orchestrator/routers/{agents,mcp,policy}.py` (multiple sites).
`agent_id` / `name` come straight from the URL path and are interpolated
into Redis keys (`agent:{id}:approved`, `mcp:{name}`, `policy:{id}`).
A request to `/agents/foo:approved/approve` writes `agent:foo:approved:approved`.
Fix: pin path params to a regex (`Path(..., regex=r"^[A-Za-z0-9_-]{1,128}$")`).

### S4-CR-6. Reply-text TaskGroup pattern is too loose
`services/orchestrator/routers/agents.py:319-323`. `_TASKGROUP_SIGNATURES`
includes the bare substring `"TaskGroup"` — a legitimate reply containing
the word gets stamped over with the humanised denial. Tighten to the full
LangChain signature, or only rewrite when a denial was found AND the reply
starts with `"Error:"`.

### S4-CR-7. Denial-attribution race
`services/orchestrator/routers/agents.py:269-316`. `_scan_recent_denials`
walks `audit:global` and returns *any* denial in the time window regardless
of which agent caused it. Concurrent denials from unrelated agents get
attached to this conversation's response. Fix: filter denials by
`agent_id ∈ {primary, primary's policy.allowed_agents}`.

### S4-CR-8. Export endpoint has no upper bound on output size
`services/orchestrator/export.py:84-123`. `_walk_records` walks the whole
stream; with `start=0`, `audit.csv` can balloon. `MAX_TRACES=1000` caps
the JSON file but the CSV is uncapped. Add `MAX_AUDIT_ROWS` cap with a
truncation indicator in the header / response.

### S4-CR-9. MCP key namespace collision
`services/proxy/enforcer.py:167`. `f"mcp:{host}"` shares namespace with
the auxiliary approval-flag key (`mcp:{name}:approved`). `routers/mcp.py:37`
already filters via `if key.count(":") != 1: continue`. Move approval flags
to a separate keyspace (`mcp_approved:{name}`) to prevent future bugs.

### S4-CR-10. `litellm.yaml` model field accepts `/`
`services/orchestrator/routers/llms.py:48`. `req.model` containing `/`
produces ambiguous `litellm_params.model` strings. Add a regex
`r"^[A-Za-z0-9_.-]+$"` on the model field.

### S4-CR-11. `api_key_ref` is unvalidated
`services/orchestrator/routers/llms.py:49`. Goes into YAML as
`api_key: os.environ/{api_key_ref}`. PyYAML quotes correctly, but
constraining to `^[A-Z][A-Z0-9_]*$` prevents surprises (newlines,
shell metacharacters, leading lowercase).

### S4-CR-12. `CONFIG_DIR` env-var injection surface
`services/proxy/policy_store.py:34`. `bootstrap_from_yaml` reads from
`CONFIG_DIR / "policies.yaml"`. Env var must be a deploy-time constant —
document or move to a CLI flag.

### S4-CR-13. Test code-injection vector via f-string
`tests/test_s4.py:82`. `model_name` interpolated into a Python script
literal. Currently safe (hardcoded), but fragile. Use parameterised
invocation (stdin or argv) instead.

### S4-CR-14. Trace-detail policy snapshot drift
`services/orchestrator/trace_detail.py`. `policy_snapshot` is fetched live
from Redis at GET time, not captured at audit-write time. Same trace
viewed 5 minutes apart can show different policies. Document as
"current policy", or capture at audit-write time (proper fix).

### S4-CR-15. ExportModal end-time clock skew tolerance
`services/ui/src/components/ExportModal.tsx`. The frontend allows `end`
up to 60s in the future as a clock-skew guard, but the orchestrator
strictly compares `end > now`. Coordinate or strict-reject in the UI.

### S4-CR-16. CSRF posture on the orchestrator
The orchestrator has zero auth on mutating endpoints (intentional per
CLAUDE.md §8 — internal network threat model). The browser GUI proxies
`/api` from `localhost:5173`, so any other site the user visits in the
same browser can `fetch('http://localhost:5173/api/policy/...', {method:'PUT'})`
and rewrite policies. Pick one: CSRF token, same-origin gate via the
`Origin` header, or a giant deployment warning.

---

# Original Sprint 1/2 follow-ups (pre-S4)

## ✅ Resolved on 2026-04-27 (code-review pass)

- **Trace_id missing on early denials** — Tracer reordered before PolicyEnforcer;
  every audit row now carries the conversation's trace_id including denials.
- **GraphEnforcer SSE redirect double-count** — split into request (atomic
  Lua-based slot reservation) + response (consume on 2xx, release on non-2xx);
  edge_loop_exceeded restored as a distinct signal under concurrency.
- **Cross-conversation trace bleed** — `trace_context:{target_sid}` now uses
  NX semantics; in-flight conversation owns target's trace context until TTL.
- **Empty alerts on policy denials** — `audit_log` synthesizes a structured
  `alert` JSON from `denial_reason` + deny envelope fields when no addon
  set one explicitly. Alerts always written on violations (CLAUDE.md §5).
- **Indirect / synthesis LLM hops** — audit records now carry `indirect=true`
  (response had `tool_calls`) and `has_tool_calls_input=true` (request had
  role=tool/function messages); displayed in dump scripts.
- **Crash-unsafe policy mutation** — `dump_audit_strict_graph.py` writes
  via tempfile + atomic `mv` and keeps a `.bak` recovery breadcrumb.

## High priority

### 1. Tool advertisement leak — proxy must filter `tools/list` responses

**Where:** `services/proxy/` (new addon or extension to `enforcer.py`)

**Problem:** When an agent calls `mcp/tools/list`, the proxy currently forwards
the upstream MCP server's full tool catalog back to the SDK unchanged. That
list is then handed to the LLM as `tools=[…]` on every chat completion. An
agent whose policy only allows `web_search` still sees `sql_query`,
`dummy_email`, `file_read` advertised — the proxy denies the calls if the LLM
tries them, but the leak is real.

**Concrete impact (observed in ST-S1.10 dump):**
- Token waste: every LLM request ships ~700 extra tokens of tool descriptions
  the agent can never use (line 25 of `tests/audit_dump_st_s1_10.txt`).
- Schema disclosure: `sql_query`'s description leaks the MySQL table layout
  (`users(id, name, email, city, age, is_active, created_at)` etc.) to OpenAI.
- Prompt-injection surface: a hostile prompt could nudge the LLM to call
  `sql_query`; only the proxy's deny stops it.

**Why fix it in the proxy, not the SDK:** SDK-side filtering breaks user code
that introspects `mcp.list_tools()` (LangChain bindings, conditional logic,
tests). Proxy-side filtering keeps the SDK's view of "available tools"
consistent with what's actually callable — the policy becomes the source of
truth for the agent's tool surface.

**Implementation sketch:**
- New `ToolListFilter` addon, runs on the response hook only when
  `flow.metadata["amaze_kind"] == "mcp"` and the request body's
  `method == "tools/list"`.
- Parse the SSE / JSON response, drop `result.tools[]` entries whose `name`
  isn't in the agent's `allowed_tools`, re-serialize.
- Empty `allowed_tools: []` → SDK sees zero tools (correct for planner
  agents that only do A2A routing).

**Tests required:**
- New ST-S2-tool-filter: agent with `allowed_tools: [web_search]` sees a
  `tools/list` response containing exactly `web_search`.
- ST-S2-tool-filter-empty: agent with `allowed_tools: []` sees an empty list.
- Regression: ST-S1.12 (NY news deny) still produces a denial when the SDK
  is patched to bypass the filter (defense-in-depth check).

---

### 2. trace_id propagation — every conversation should have ONE trace_id

**Status:** ✅ Fix landed 2026-04-26 (patched live container). Needs to be
baked into the Dockerfile build pipeline (see #4).

**What was wrong:** Each outbound HTTP request from an agent (LLM, every MCP
frame, A2A) became its own root span with a fresh trace_id. ST-S1.10 produced
**14 different trace_ids on `agent-sdk` for a single conversation**.

**Root cause:** The SDK doesn't open a parent span on inbound `/chat`, so
outbound requests carry no `traceparent` header. The proxy's `Tracer` addon
started a new root span per request.

**Fix applied:** In `services/proxy/tracer.py`, the addon now stores the first
span's traceparent under `trace_context:{session_id}` (Redis, 24h TTL) and
reuses it as the parent context for every subsequent call in the same session.
All calls in one conversation now share a trace_id.

**Follow-up still open:**
- True root-span model — SDK should open a request-scoped parent span on
  inbound `/chat` and propagate via `traceparent`. The current proxy fix is a
  pragmatic shortcut; the SDK fix would let upstream OTel collectors see a
  proper parent-child hierarchy with one root per conversation.
- A2A propagation already worked via Redis cross-agent context lookup.

---

### 3. Audit log structured fields are empty on early denials

**Where:** `services/proxy/audit_log.py` + `services/proxy/enforcer.py`

**Problem:** When `PolicyEnforcer` calls `deny()` early (e.g.
`tool-not-allowed`), the audit record's structured `kind`, `tool`, and
`target` fields end up empty — the data is only in the `input` payload and
`denial_reason`. Example from ST-S1.12 dump:

```
kind=unknown  tool=""  target=""
denial_reason=tool-not-allowed
input={"method":"tools/call","params":{"name":"dummy_email",...}}
```

This makes audit queries by tool name unreliable. Sprint 3's traces UI will
filter by tool/kind — needs these populated.

**Fix:** `PolicyEnforcer` must set `amaze_kind`, `amaze_mcp_server`,
`amaze_mcp_tool`, `amaze_target` BEFORE calling `deny()`. Currently it sets
some of these on the allow path only.

---

### 4. Dockerfile uses dead Jaeger v1.57.0 download URL

**Where:** `Dockerfile` line 17

**Problem:** `https://github.com/jaegertracing/jaeger/releases/download/v1.57.0/jaeger-all-in-one-1.57.0-linux-amd64.tar.gz`
returns 404 — the GitHub release was removed. Clean rebuilds of the platform
container fail.

**Fix:** Bump to a current Jaeger release (e.g. v1.62.0 or later) and verify
the asset URL matches the current naming scheme.

**Why this matters now:** I had to patch the running container manually with
`docker cp` to land Sprint 2 proxy code (`audit_log.py`, `graph_enforcer.py`,
`stream_blocker.py`, `tracer.py`). That's not a sustainable workflow — the
next clean rebuild will fail until this is fixed.

---

### 5. GraphEnforcer counts MCP `tools/call` SSE response as a second tool call

**Where:** `services/proxy/graph_enforcer.py`, in coordination with `audit_log` /
mitmproxy flow lifecycle.

**Discovered by:** `tests/dump_audit_strict_graph.py` (ST-S1.14) — bitcoin
scenario under a strict graph `web_search → agent-sdk1`.

**Problem:** When an MCP server like demo-mcp uses SSE for tool responses, the
proxy sees TWO flows for one logical `tools/call` invocation:
  1. `POST /mcp` with the JSON-RPC body → tool=web_search (the request)
  2. `GET /mcp` (or similar) returning the `event: message data: {...}` chunk
     → also tool=web_search (the response stream)

GraphEnforcer treats both as separate "tool call" steps. With `max_loops=1`,
the first hits step 1, advances to step 2, and the second hits step 2 with
the wrong callee_id → **graph_violation**.

Audit excerpt from ST-S1.14:
```
[ 6] mcp  allow   tool=web_search   tools/call request          ← step 1 advance
[ 7] mcp  DENIED  tool=web_search   tools/call SSE response     ← step is now 2 → graph_violation
```

The conversation breaks: agent-sdk's LLM never gets the search results, no
A2A routing happens, no audit on agent-sdk1.

**Fix options:**
- A. Track flows by their MCP request_id (`jsonrpc.id`) so GraphEnforcer only
  counts each logical `tools/call` once. Requires correlating request+response
  in the addon (mitmproxy provides `flow.id` for this).
- B. GraphEnforcer should only run on the REQUEST hook, not on response/SSE
  follow-up flows. Look at `flow.request.method` — only POST `tools/call`
  should advance the graph.
- C. Stop emitting a separate audit record for the SSE response leg (related
  to TODO item #6 below). If the SSE response shares the same flow.id, no
  second graph step would be counted.

Option B is the cleanest single change — the SSE / GET callback isn't really
a "tool invocation" semantically.

**Tests to add:**
- ST-S1.14 (above) should pass: bitcoin under strict graph completes the
  web_search → agent-sdk1 → finish path without graph_violation.
- ST-S2.* edge_loop_exceeded test should still trip on a genuine repeated
  invocation (same tools/call sent twice).

---

### 6. Audit noise — keepalive / handshake frames have empty input+output

**Where:** `services/proxy/audit_log.py`

**Problem:** Every CONNECT and SSE keepalive frame produces an audit record
with empty `input` and `output`. ST-S1.10's `agent-sdk` dump shows ~5 such
records per conversation (lines 3, 4, 10, 11, 12 of
`tests/audit_dump_st_s1_10.txt`).

**Fix:** Skip audit XADD when both `input` and `output` are empty AND the
flow is not a denial. Keep all denials regardless.

---

## Lower priority

### 7. Each MCP `tools/call` produces TWO audit records (request + SSE response chunk)

The pairing inflates record counts and makes the traces UI look noisier than
it should. Worth merging into one record at audit time, or de-duplicating in
the UI query layer.

### 8. Container Redis exposed only via `docker exec`

For test infrastructure, having Redis reachable on a host port (or a
dedicated `tests/` compose override) would simplify pytest setup. Currently
`tests/test_s1_integration.py` shells into the platform container for every
Redis read.
