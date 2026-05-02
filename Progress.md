# Progress

Running log. Newest at top.

---

## 2026-04-30 (sprint close) — Sprint S4: COMPLETE

### Phase 6 + sprint-close code review

- T6.1 ✓ — `GET /export` (212 + 91 LOC); ZIP with traces.json + audit.csv
- T6.2 ✓ — `ExportModal.tsx` + `api/exports.ts` (453 + 53 LOC); wired to Traces tab
- T6.3 ✓ — `tests/test_s4_export.py` (8 tests), code-review pass, demo report

### Code-review findings — sprint-close pass

20 items raised (4 blocking, 12 should-fix, 4 nit).

**Fixed in-sprint:**
1. SSRF in `POST /agents/{id}/messages` — chat_endpoint now URL-validated
   (scheme http(s) + non-empty host required); 409 on invalid value.
2. Cross-conversation per-turn reset — narrowed from "scan-all" to
   `{primary_sid} ∪ {peer_sid for peer in policy.allowed_agents}`. Concurrent
   conversations on different agents no longer wipe each other.
3. Header injection in export filename — `agent` query param sanitised via
   `re.sub(r"[^A-Za-z0-9_.-]", "_", label)` before insertion into
   Content-Disposition.
4. `POST /llms` base_url validation — added `field_validator` requiring
   http(s)://host (matches the existing `mcp.py` pattern).

**Deferred to TODO.md (S4-CR-5 through S4-CR-16):**
12 should-fix items — path-param regex constraints, denial-attribution
race, export row cap, MCP key namespace cleanup, model-field regex,
api_key_ref regex, CSRF posture, ExportModal clock-skew, etc. Captured
with file:line references and rationale.

### Final test state

```
pytest tests/test_s4.py tests/test_s4_export.py
→ 25 passed, 2 skipped, 0 failed in 33s
```

The 2 skipped: ST-S4.18 (alerts donut UI integration — requires browser
harness) and a stub ST-S4.19 (export integration is fully covered by
the 8 tests in test_s4_export.py instead).

### Sprint S4: 26/26 tasks complete

| Phase | Tasks | Wall clock |
|---|---|---|
| 1 Foundation | 5 ✓ | ~10 min |
| 2 Approvals + policy persist | 7 ✓ | ~3.5 min |
| 3 Editor + send + trace + tests | 6 ✓ | ~10 min |
| 4 Stats backend | 3 ✓ | ~2 min |
| 5 Charts + alerts UI | 4 ✓ | ~4.5 min |
| 6 Export + code review + demo | 3 ✓ | ~5 min |

Sprint demo report: `reports/demo_s4_gui.md`.

---

## 2026-04-30 (late evening) — Sprint S4 Phases 3-5 complete

### Phase 3 close — T3.6 system tests (15/15 passed first run)

`tests/test_s4.py` (414 LOC) covering 16 of 19 signed-off tests against the
live `amaze-platform`. 4 deferred to later phases:
- ST-S4.13/14 → activated in Phase 4 (now passing)
- ST-S4.18 → browser-required alerts donut integration; pending walkthrough
- ST-S4.19 → export, Phase 6

`AMAZE_ORCHESTRATOR=http://localhost:8001 pytest tests/test_s4.py -v`:
**17 passed, 2 skipped in 0.87s.**

### Phase 4 — Stats backend (3 tasks, all parallel)

| Task | Files | LOC |
|---|---|---|
| T4.1 ✓ | `services/orchestrator/stats.py` (new) | 424 |
| T4.2 ✓ | `routers/agents.py` (+`GET /agents/{id}/stats`) | +65 |
| T4.3 ✓ | `routers/alerts.py` (new), `main.py` (mount) | 178 |

Live data from the live container (24h range):
- `/agents/agent-sdk/stats` → 176 calls, 1 unique tool, 3 critical alerts, policy_health=warn, 24-point time series with real bucketing
- `/alerts?range=24h&groupBy=reason` → 7 total: tool-not-allowed (3), rate-limit-exceeded (2), agent-limit-exceeded (1), invalid-bearer (1)

### Phase 5 — Charts + alerts UI (4 tasks, all parallel)

| Task | Files | LOC |
|---|---|---|
| T5.1 ✓ | `pages/AgentDashboard.tsx` (new), `api/stats.ts` (new), `api/agents.ts` (sendAgentMessage), `pages/Agents.tsx` (route by state), `main.tsx` (route) | ~750 |
| T5.2 ✓ | `components/LineChart.tsx` (new) | 268 |
| T5.3 ✓ | `components/DonutChart.tsx` (new) | 380 |
| T5.4 ✓ | `pages/Alerts.tsx` (rewrite), `api/alerts.ts` (new) | ~400 |

UI build: 1727+ modules → 220 kB gzip JS, TypeScript clean.

### Per-turn semantics widened

ST-S4.12 caught a real issue: original per-turn reset only cleared the
PRIMARY agent's session counters. agent-sdk2's session counters kept
accumulating across messages because send-message only knew about the
primary recipient. After a few turns, agent-sdk2 hit `max_tokens_per_turn`
and the demo broke. Reset now covers ALL `session:*:total_*`,
`graph:*`, and `trace_context:*` keys at the start of each send-message.

Documented constraint: this is correct for single-conversation demos.
For multi-tenant production, replace session-scoped state with
conversation-scoped state.

### Sprint timing — Phases 3-5

| Phase | Wall-clock | Tasks | Notes |
|---|---|---|---|
| Phase 3 (T3.1–5 parallel) | ~5 min | 5 | per-turn reset + denial translation follow-ups inline |
| Phase 3 close (T3.6 tests) | ~4.7 min | 1 | 15/15 first try |
| Phase 4 + 5 (parallel) | ~4.5 min | 7 | all 7 launched in single message |

### What's now demo-able end-to-end

1. Sidebar → 5 tabs all functional with live data
2. **Agents tab** → click `approved-with-policy` row → dashboard with
   KPIs, line chart (calls/latency toggle), 4 donuts, send-message
   composer, recent traces
3. **Agents tab** → click `approved-no-policy` row → policy editor
   (Strict mode → graph canvas; Flexible → checklists)
4. **MCP Servers tab** → register / approve / reject; tools as chips
5. **LLM Providers tab** → Add LLM modal with live YAML preview
6. **Traces tab** → live list, click → full-page detail with sequence
   diagram + edges table + violations + policy snapshot
7. **Alerts tab** → big donut by reason, click slice / pill / URL
   query → filter records table, row click → trace detail
8. Cross-page deep links: dashboard donut click → /traces or /alerts pre-filtered

---

## 2026-04-30 (evening) — Sprint S4 Phase 3 (T3.1–T3.5) complete

### What was done

5 tasks in 5 parallel agents:

| Task | Files | LOC |
|---|---|---|
| T3.1 ✓ | `pages/AgentPolicy.tsx` (new), `api/policy.ts` (extended), `pages/Agents.tsx` (clickable rows), `main.tsx` (route) | ~600 |
| T3.2 ✓ | `components/GraphEditor.tsx` (new), `components/StepNode.tsx` (new), `api/policy.ts` (types) | 682 + 91 |
| T3.3 ✓ | `routers/agents.py` (POST /agents/{id}/messages) | +80 |
| T3.4 ✓ | `trace_detail.py` (new), `routers/traces.py` (GET /traces/{trace_id}) | ~250 |
| T3.5 ✓ | `pages/Traces.tsx` (full rewrite), `pages/TraceDetail.tsx` (new), `components/SequenceDiagram.tsx` (new), `api/traces.ts` (new), `main.tsx` (route) | 235 + 584 + 207 + 112 |

### T3.3 follow-ups landed in Phase 3

**Turn-semantics correction (CLAUDE.md §3 + §5).** Pre-S4 turn = session
lifetime — counters and graph state never reset. The GUI's click-to-message
flow needs each user message to be a fresh turn. Send-message now clears
at the start of every call:

- `trace_context:{sid}` — fresh trace_id per message
- `session:{sid}:total_tokens` — fresh `max_tokens_per_turn` budget
- `session:{sid}:total_tool_calls` — fresh `max_tool_calls_per_turn` budget
- `session:{sid}:total_agent_calls` — fresh `max_agent_calls_per_turn` budget
- `graph:{sid}:*` (scan_iter) — current_step pointer + per-step loop counters
  reset so strict-mode agents start from `graph.start_step`

Time-windowed rate limits (`ts:{agent_id}:llm_tokens` etc.) are NOT reset —
they're decay-based by design.

**Human-readable denials.** When the proxy denies a call, LangChain wraps
the 403 in a generic "unhandled errors in a TaskGroup" exception that
hides the actual reason. Send-message now scans `audit:global` for denials
within the call's time window and adds a `denial` field to the response:

```json
{
  "denial": {
    "reason": "tool-not-allowed",
    "human": "Tool 'dummy_email' on 'demo-mcp' is not in this agent's policy.allowed_tools",
    "alert": { "tool": "dummy_email", "server": "demo-mcp", ... }
  }
}
```

The translator covers tool-not-allowed, agent-not-allowed, llm-not-allowed,
graph_violation, edge_loop_exceeded, budget_exceeded, rate-limit-exceeded,
invalid-bearer, mcp-not-allowed, host-not-allowed, policy-not-found,
redis-unavailable. If the agent's reply text matches the TaskGroup signature,
the orchestrator REPLACES the agent's reply with `"Denied: <human>"` so the
user sees something actionable.

### T3.3 follow-up — chat_endpoint registration

The send-message agent (T3.3) used `agent:{id}:endpoint` (the A2A port,
9002) and POSTed to `/chat`. **Wrong port** — the SDK serves user prompts on
the chat port (default 8080, mapped to host 8090) which is a separate
FastAPI app. Fixed:

- send-message now reads `agent:{id}:chat_endpoint` first
- 409 `agent-chat-port-unregistered` if A2A registered but chat port not
- Manually populated `agent:agent-sdk:chat_endpoint = host.docker.internal:8090`
  for the live demo agent

**Follow-up for a later sprint:** SDK should auto-register chat_endpoint
during `Config.register()` — currently only A2A endpoint is registered.

### Smoke-test results

| Endpoint | Result |
|---|---|
| `GET /traces/{trace_id}` (existing fossil trace) | Full assembly returned: prompt extracted, failure_details=tool-not-allowed, policy_snapshot from Redis, metrics + sequence_steps + edges all populated ✓ |
| `GET /traces/garbage_id` | 404 trace-not-found ✓ |
| `POST /agents/agent-sdk/messages` | 200, agent's reply returned, trace_id retrieved (best-effort latest from `audit:agent-sdk` stream) ✓ |
| UI build | 1 727+ modules, 6.98s, 211 kB gzip — TypeScript clean ✓ |

### Sprint timing — Phase 3

| Task | Duration | Tokens |
|---|---|---|
| T3.3 (send-message) | 1 min | ~44 k |
| T3.4 (trace_detail) | 2.8 min | ~54 k |
| T3.2 (graph canvas) | 3.5 min | ~72 k |
| T3.5 (trace UI) | 4.6 min | ~72 k |
| T3.1 (policy editor) | 4.6 min | ~87 k |
| T3.3 chat_endpoint follow-up | inline | — |

Wall clock: ~5 min (parallel, slowest agent gates).

### What's now demo-able end-to-end

1. Browse agents in the GUI (live data from `GET /agents`)
2. Click a row → policy editor (`/agents/:id/policy`)
3. Toggle strict mode → graph editor renders, drag-to-connect, validate
4. Save → `PUT /policy/:id` → next proxy call uses new policy (live update)
5. Browse traces in the GUI (live data from `GET /traces`)
6. Click a trace → full-page detail with SVG sequence diagram, edges table,
   policy snapshot, violations
7. Click an arrow in the sequence diagram → corresponding edge row highlights
8. Send a message via UI → agent processes → trace appears in Traces tab

T3.6 (system tests) is the remaining Phase 3 task before moving to Phase 4.

---

## 2026-04-30 (afternoon) — Sprint S4 Phase 2 complete

### What was done

7 tasks in 5 parallel agents (4 backend disjoint files + 2 UI disjoint pages):

| Task | Files | Notes |
|---|---|---|
| T2.1 ✓ | `routers/agents.py`, `routers/mcp.py` | 4 approve/reject endpoints |
| T2.2 ✓ | `services/proxy/policy_store.py` (new), `policy.py`, `enforcer.py`, `routers/policy.py` (new), `main.py` | **Policy Redis-primary.** YAML bootstrap idempotent (SETNX). Enforcer refetches per-request. `GET/PUT /policy/{id}` |
| T2.3 ✓ | `routers/llms.py`, `config/litellm.yaml` (created on first POST) | Atomic YAML write via tempfile + os.replace |
| T2.4 ✓ | `routers/mcp.py` | `POST /mcp_servers` manual register, 409 on duplicate |
| T2.5 ✓ | `pages/{Agents,McpServers}.tsx`, `api/{agents,mcp}.ts` | Live `useQuery` with 5s refetch, mutation invalidation, per-row spinners |
| T2.6 ✓ | `pages/Llms.tsx`, `components/AddLlmModal.tsx`, `api/llms.ts` | Live YAML preview pane updates as user types |
| T2.7 ✓ | `components/AddMcpModal.tsx` | Validation, 409 inline error |

### Follow-up landed in same pass (not in original task list)

T2.2 agent flagged that `counters.py` and `graph_enforcer.py` still cached
policies at boot — would have broken the live-update guarantee for budget
+ graph enforcement after `PUT /policy`. Patched both to use
`policy_store.get_policy()` per request, matching `enforcer.py`. Now ALL
three policy consumers refetch. ST-S4.16 (live policy update) is deliverable.

### Smoke test results (against patched amaze-platform)

| Endpoint | Result |
|---|---|
| Bootstrap on first restart | 15 `policy:*` keys written from `policies.yaml` (idempotent re-run skipped existing) |
| `POST /agents/agent-sdk/approve` | `{agent_id, approved: true}` ✓ |
| `POST /mcp_servers/demo-mcp/reject` → `GET /mcp_servers` | `approved: false` reflected immediately ✓ |
| `POST /mcp_servers` (manual) | 201 with `approved: true` ✓ |
| `POST /mcp_servers` (duplicate name) | 409 `mcp-already-exists` ✓ |
| `POST /llms` then `GET /llms` | New entry visible; `litellm.yaml` written atomically ✓ |
| `GET /policy/agent-sdk` | Full policy returned from Redis (bootstrapped) ✓ |
| Side effects reverted | demo-mcp re-approved, smoke-mcp removed, litellm.yaml removed ✓ |
| UI build | 1 727 modules, 5.44s, 140 kB gzip — TypeScript clean ✓ |

### Sprint timing — Phase 2

| Task | Duration | Tokens |
|---|---|---|
| T2.1+T2.4 (approve + add MCP) | 1.3 min | ~39 k |
| T2.2 (policy Redis-primary) | 2.7 min | ~59 k |
| T2.3 (add LLM + YAML emit) | 1 min | ~35 k |
| T2.5+T2.7 (UI approvals + AddMcpModal) | 3.4 min | ~63 k |
| T2.6 (UI Add LLM modal) | 2.5 min | ~55 k |
| Cache-elimination follow-up + smoke | inline | — |

Wall clock for Phase 2: ~3.5 min (parallel, slowest agent gates).

### Bootstrap mechanics (locked)

- `services/proxy/policy_store.bootstrap_from_yaml()` runs in orchestrator
  startup (`_lifespan`).
- For each agent in `config/policies.yaml`: if Redis `policy:{agent_id}`
  is absent, write it via `SETNX`. Existing Redis values are NEVER
  overwritten — UI edits win on subsequent runs.
- `enforcer.py`, `counters.py`, `graph_enforcer.py` all call
  `await policy_store.get_policy(agent_id)` per request. No in-memory cache.
- `PUT /policy/{id}` writes Redis directly. Next request through the proxy
  picks up the new policy (live update verified).

---

## 2026-04-30 — Sprint S4 kickoff: Phase 1 complete

Sprint S4 plan landed in `SPRINTS.md` (6 phases, 25 tasks, 19 system tests
signed off). Tool-advertisement filter deferred — stays in `TODO.md` #1.

### Phase 1 — Foundation (T1.0 sign-off + 4 tasks)

| Task | Files | LOC |
|---|---|---|
| T1.0 ✓ | system-test list signed off | — |
| T1.1 ✓ | `services/ui/` (React 19 + Vite 5 + TS + Tailwind + Radix + XYFlow skeleton, 5 placeholder pages, dark theme matching mock) | 31 files / 1 085 |
| T1.2 ✓ | `services/orchestrator/routers/{agents,mcp,llms,traces}.py`, `main.py` mount | ~250 |
| T1.3 ✓ | `services/orchestrator/audit_query.py` (XREVRANGE wrappers, trace projection, cursor pagination) | 433 |
| T1.4 ✓ | `Dockerfile.ui` (multi-stage Node→nginx), `services/ui/nginx.conf`, `docker-compose.yml` add `amaze-ui` service on host port 5173 | — |

### Parallelism

T1.1 + T1.2 + T1.3 launched in parallel (3 background agents, file-disjoint).
T1.2 stubbed `audit_query.py` first; T1.3 overwrote with the real impl using
the locked interface contract. T1.4 ran sequentially as wave 2 since it
depends on T1.1's package.json.

### Sprint timing — Phase 1

| Task | Duration | Tokens |
|---|---|---|
| T1.1 (UI skeleton) | 8 min | ~74 k |
| T1.2 (read endpoints) | 1.5 min | ~46 k |
| T1.3 (audit_query.py) | 2.5 min | ~42 k |
| T1.4 (Dockerfile.ui + compose) | <1 min | inline |

Wall clock for Phase 1: ~10 min (parallel limit is the slowest agent: T1.1).

### Endpoints now live (read-only)

- `GET /agents` — state classification: pending / approved-no-policy / approved-with-policy
- `GET /mcp_servers` — name, url, tools, approved
- `GET /llms` — LiteLLM model_list (returns `[]` if `config/litellm.yaml` missing)
- `GET /traces?agent=&limit=&offset=` — paginated trace summaries

Existing endpoints (`/health`, `/register`, `/resolve/*`) untouched.

### Notes

- `audit_query.py` cursor uses decremented stream-id (`ms-(seq-1)`) to avoid
  the inclusive-XREVRANGE re-read on page boundaries.
- Vite dev proxy → `localhost:8001`; production nginx proxy → `amaze:8001`
  (Docker DNS).
- UI skeleton uses HSL dark-theme tokens for shadcn primitives PLUS the raw
  hex tokens from the mock — kept dual so Sidebar/pages can mirror the mock
  pixel-for-pixel while Radix primitives stay shadcn-compatible.

---

## 2026-04-29 (evening) — S3 infrastructure fixes + ST-S3.8 end-to-end

### What was done

**Infrastructure fixes (all blocking, discovered while running ST-S3.8):**

| # | Problem | Fix | Files |
|---|---|---|---|
| F1 | `supervisord.conf` hardcoded `REDIS_URL=redis://127.0.0.1:6379` overrode compose-level `REDIS_URL=redis://amaze-redis:6379` — proxy/orchestrator wrote to a non-existent Redis while tests read from the compose Redis | Replaced hardcoded URL with `%(ENV_REDIS_URL)s` supervisord interpolation in both `[program:orchestrator]` and `[program:proxy]` | `docker/supervisord.conf` |
| F2 | `docker-compose.yml` passed `REDIS_URL: redis://host.docker.internal:6379` — Redis is bound to `127.0.0.1:6379` on host, unreachable from inside Docker via `host.docker.internal` | Changed to `REDIS_URL: redis://amaze-redis:6379` (Docker DNS) | `docker/docker-compose.yml` |
| F3 | Jaeger download URL 404 — naming convention changed in v1.57.0 (`jaeger-all-in-one-1.57.0-linux-amd64.tar.gz` → `jaeger-1.57.0-linux-amd64.tar.gz`) | Fixed URL + tar path in Dockerfile | `Dockerfile` |
| F4 | Audit log recorded all MCP protocol negotiation (initialize, notifications, tools/list, etc.) — noise swamped business calls | Added filter: `if kind == "mcp" and not tool and not denied: return` — only `tools/call` and denied MCP calls are recorded | `services/proxy/audit_log.py` |

**New files:**

| File | Purpose |
|---|---|
| `Dockerfile.incremental` | Overlay updated code onto existing base image — skips Jaeger re-download; copies `services/`, `config/`, `supervisord.conf` |
| `Dockerfile.agent.incremental` | Overlay updated SDK source onto existing agent image; parameterised via `FROM_IMAGE` build arg |
| `scripts/load-base-image.sh` | Load saved platform base image from `/home/ubuntu/docker-images/amaze-platform-base.tar.gz` on a fresh system |

**ST-S3.8 result:**

Run clean after fixes. Trace ID `482263540ac46121fcaab9ecd72aca45`.

| Metric | Value |
|---|---|
| Audit records | 6 (5 agent-sdk + 1 agent-sdk1) |
| Spans in Jaeger | 16 (6 business + 10 MCP protocol) |
| LLM calls | 3 (320 + 735 + 375 tokens = 1430 total) |
| MCP tool calls | 2 × web_search |
| A2A calls | 1 (agent-sdk → agent-sdk1) |
| Wall clock | ~8.3 s |

**Reports generated:**
- `reports/st_s3_8_audit.md` — 6 audit records, full fields, no truncation, no MCP noise, trace_id on every record
- `reports/st_s3_8_trace.md` — 16 spans; A2A split into request / agent-sdk1-LLM / response sections so reading order matches execution order

---

## 2026-04-29 (evening, continued) — ST-S3.9, ST-S3.10, code review + fixes

### Tests run

| Test | Prompt | Result |
|---|---|---|
| ST-S3.9 | "search for current weather in London" | 200 — "sunny 15.3°C, ENE wind 16.6 mph, humidity 36%" |
| ST-S3.10 | "email me the current NEW YORK news" | Error: TaskGroup (dummy_email denied 403 tool-not-allowed) |

**ST-S3.9 stats:** 9 audit records (5 agent-sdk + 4 agent-sdk2), 39 spans, 2935 total tokens.
Both agents independently searched London weather — agent-sdk resolved it first, then A2A'd
the result to agent-sdk2 which re-searched and synthesized its own answer.

**ST-S3.10 stats:** 2 audit records, 7 spans, 1 denial. agent-sdk's LLM ignored the
"web_search only" system prompt and tried dummy_email directly. Proxy denied it immediately
(0.9 ms); routing to agent-sdk2 never happened. Error propagated back to user.

### Reports generated

| File | Lines |
|---|---|
| `reports/st_s3_9_audit.md` | 621 |
| `reports/st_s3_9_trace.md` | 152 |
| `reports/st_s3_10_audit.md` | 80 |
| `reports/st_s3_10_trace.md` | 165 |

### Code review findings (S3 finish)

| # | Severity | File | Finding | Status |
|---|---|---|---|---|
| R1 | 🟡 | `services/proxy/enforcer.py` | `amaze_kind`/`amaze_mcp_server` set after early-deny `return` on tool-not-allowed → audit logged `kind=unknown` for denied MCP tool calls | ✓ Fixed |
| R2 | 🟡 | `docker/docker-compose.yml` | Port comment said "reaches Redis via host.docker.internal:6379" — contradicts redis-dns fix | ✓ Fixed |
| R3 | 🟡 | `Dockerfile.agent.incremental` | Comment "no internet required" didn't explain the editable-install dependency | ✓ Fixed |
| R4 | 🟢 | `audit_log.py` | `raw_input` truncated to 2000 chars but `req_body` parses full content — needs comment | deferred nit |
| R5 | 🟢 | `Dockerfile` | `EXPOSE` missing ports 16686 and 4317 (Jaeger) | deferred nit |

### Base image re-saved ✓
`/home/ubuntu/docker-images/amaze-platform-base.tar.gz` updated to current
`amaze-amaze:latest` (includes Jaeger + enforcer fix, 51 MB).

### Sprint timing (S3 finish session)

| Task | Start | End | Duration |
|---|---|---|---|
| Base image re-save | 18:37 | 18:37 | <1 min |
| ST-S3.9 run + reports | 18:38 | 18:44 | 6 min |
| ST-S3.10 run + reports | 18:44 | 18:45 | 1 min |
| Code review | 18:45 | 18:50 | 5 min |
| Enforcer fix + rebuild + verify | 18:50 | 18:57 | 7 min |
| Progress.md + SPRINTS.md update | 18:57 | 19:00 | 3 min |

### Sprint S3 status: COMPLETE ✓
All 10 system tests green (ST-S3.1–ST-S3.10).

---

## 2026-04-29 — Sprint S3 complete: Remote Agent + MCP Runtime MVP

All 7 S3 tasks delivered. 9 system tests collected clean.

### Deliverables

| Task | Description | Files |
|---|---|---|
| S3-T1 ✓ | Architecture doc | `docs/remote-routing-architecture.md` |
| S3-T2 ✓ | Orchestrator registration API | `services/orchestrator/main.py` |
| S3-T3 ✓ | SDK endpoint registration | `sdk/amaze/_core.py` |
| S3-T4 ✓ | Router addon | `services/proxy/router.py`, `services/proxy/main.py` |
| S3-T5 ✓ | Compose + demo script | `examples/compose.yml`, `docker/compose-agent-host.yml`, `docker/compose-mcp-host.yml`, `scripts/demo_remote_runtime.sh` |
| S3-T6 ✓ | System tests | `tests/test_s3.py`, `tests/conftest.py`, `config/policies.yaml` |
| S3-T7 ✓ | Sprint demo report | `reports/demo_remote_runtime.md` |

### Sprint timing

| Task | Start | End | Duration | Notes |
|---|---|---|---|---|
| S3-T1 (arch doc) | 14:00 | 14:05 | 5 min | Sequential (Phase 0) |
| S3-T2+T3+T5 | 14:05 | 14:20 | 15 min | Parallel (Phase 1, 3 agents) |
| S3-T4 (Router) | 14:20 | 14:30 | 10 min | Sequential (Phase 2, deps on T2) |
| S3-T6 (tests) | 14:30 | 14:50 | 20 min | Sequential (Phase 3) |
| S3-T7 (demo) | 14:50 | 14:55 | 5 min | Sequential (Phase 4) |
| Code review + fixes | 14:55 | 15:10 | 15 min | 1 blocking bug fixed (NameError) |

### Code review findings (S3)

| # | Severity | File | Fix applied |
|---|---|---|---|
| R1 | 🔴 | `tests/test_s3.py` | Removed undefined `UNREGISTERED_AGENT` reference (line 351) — would NameError at runtime |
| R2 | 🟡 | `tests/test_s3.py` | Tightened `/resolve/agent/` 404 assertion to exact `detail == "agent-not-registered"` |
| R3 | 🟡 | `tests/test_s3.py` | Simplified redundant counter check in ST-S3.2 |
| R4 | 🟢 | `tests/conftest.py` | Added inline comments explaining which tests each endpoint registration serves |

### Key architectural decision

Agents register their reachable `host:port` at startup (via `AMAZE_A2A_HOST` +
`AMAZE_A2A_PORT` env vars). The proxy's new Router addon (last in chain)
reads `agent:{id}:endpoint` from Redis and rewrites `flow.request.host + port`
before opening the upstream connection. `HTTP_PROXY` / `HTTPS_PROXY` and all
existing addons are unchanged. Audit records always use logical names.

### Backward compatibility

S2 tests are fully compatible. A new `tests/conftest.py` (autouse session
fixture) registers `test-a2a-callee`'s endpoint before any test runs. This
prevents the Router from denying S2 A2A tests with 503 `agent-not-registered`.

---

## 2026-04-25 — Sprint S2 code-review pass

`/code-reviewer` run on all S2 files. 1 blocking + 6 should-fix items applied.

| # | Severity | File | Fix |
|---|---|---|---|
| R1 | 🔴 | `services/proxy/graph_enforcer.py` | Atomic `INCR`+`EXPIRE` for loop counter — replaced non-atomic `GET`+`SET` that allowed concurrent flows to bypass `max_loops` |
| R2 | 🟡 | `services/proxy/counters.py` | Per-turn INCRs and TS.ADD (tool/a2a/llm) gated on `status_code < 400` — denied requests no longer inflate enforcement counters or dashboards |
| R3 | 🟡 | `services/proxy/enforcer.py` | Per-turn budget Redis error → 503 deny (was fail-open skip) |
| R4 | 🟡 | `services/proxy/counters.py` | Rate-limit pre-check Redis error → 503 deny (was fail-open skip) |
| R5 | 🟡 | `services/proxy/policy.py` | `Graph` `@model_validator` validates `start_step` exists and all `next_steps` ref valid step IDs at load time |
| R6 | 🟡 | `services/proxy/policy.py` | `RateLimit.window` `@field_validator` rejects non-`\d+[smh]` values at load time |
| R7 | 🟡 | `services/proxy/tracer.py` | OTel endpoint reads `OTEL_EXPORTER_OTLP_ENDPOINT` env var (default `localhost:4317`) |
| R8 | 🟡 | `tests/test_s2.py` | `audit_wait` default timeout 4 s → 8 s |

Bugs caught by the review that were already fixed in T2-12 authoring (not re-fixed):
`graph_enforcer.py` key name, `audit_log.py` missing await, `audit_log.py` violation alert field.

---

## 2026-04-24 — Sprint S2 T2-12 system tests

13 system tests (ST-S2.1 – ST-S2.13) written and infrastructure in place.

### Bugs fixed during test authoring

| # | File | Fix |
|---|---|---|
| B1 | `services/proxy/graph_enforcer.py:29` | `amaze_agent_id` → `amaze_agent` (key mismatch vs. session.py — GraphEnforcer was silently skipping all checks) |
| B2 | `services/proxy/audit_log.py:81` | `r = redis_client()` → `r = await redis_client()` (missing await → coroutine object, audit writes silently failed) |
| B3 | `services/proxy/audit_log.py:58` | Added `amaze_violation` to `alert_data` so graph violations in allow-mode appear in the audit `alert` field |

### Files added / changed

| File | What |
|---|---|
| `config/policies.yaml` | 9 test agent policies (`test-strict`, `test-strict-allow`, `test-strict-loop`, `test-flexible`, `test-low-tokens`, `test-low-tools`, `test-rate-limit`, `test-a2a-caller`, `test-a2a-callee`) |
| `tests/mock_mcp.py` | Generic HTTP 200 responder — used as both mock-mcp and mock-agent in test compose |
| `tests/mock_llm.py` | OpenAI-format mock returning `total_tokens:25` — aliased as `api.openai.com` on amaze-egress |
| `tests/compose.test.yml` | Self-contained test stack (platform + redis-stack + mock-mcp + mock-agent + mock-llm); exposes 18001/18080/16379/16686 |
| `tests/test_s2.py` | 13 system tests; no real LLM key required |
| `tests/requirements.txt` | `pytest`, `httpx`, `redis[hiredis]` |

### Test infrastructure highlights

- `mock-llm` given DNS alias `api.openai.com` on `amaze-egress` → proxy LLM forwarding intercepted without real API key
- Per-turn budget tests (ST-S2.7, ST-S2.8, ST-S2.9) use pre-seeded Redis counters/TS for determinism
- Cross-agent trace test (ST-S2.10) verifies `trace_context:{sid}` Redis key AND equal `trace_id` in both agents' audit records

---

## 2026-04-24 — Sprint S2 implementation (parallel execution)

All S2 backend tasks completed in four parallel phases.

### Files changed

| Task | Files |
|---|---|
| T2-1 | `docker/docker-compose.yml`, `docker/compose.networks.yml`, `docker/supervisord.conf`, `Dockerfile`, `services/proxy/_redis.py` |
| T2-7 | `services/proxy/policy.py`, `services/proxy/enforcer.py`, `config/policies.yaml` |
| T2-11b | `services/proxy/stream_blocker.py` (new) |
| T2-6 | `Dockerfile`, `docker/docker-compose.yml`, `docker/supervisord.conf` |
| T2-9 | `services/proxy/counters.py` |
| T2-10 | `services/proxy/audit_log.py` (new) |
| T2-8 | `services/proxy/graph_enforcer.py` (new) |
| T2-11 | `services/proxy/tracer.py` (new), `requirements.txt` |
| Wire | `services/proxy/main.py` — 7-addon chain |

### Policy model change
`on_violation: alert` renamed to `on_violation: allow`; alert always written to audit log; setting only controls blocking.

---

## 2026-04-22 — Sprint S1 code review + fix pass (commit bc8ce48)

Code-reviewed every file landed in S1; applied 5 critical + 4 should-fix.

| # | Where | Fix |
|---|---|---|
| C1 | `services/proxy/session.py` | Strip `X-Amaze-Bearer` on ALL paths including bypass |
| C2 | `services/orchestrator/main.py` | Migrate to FastAPI `lifespan` (deprecation) |
| C3 | `services/proxy/enforcer.py` | `_lookup_mcp` fail-closed on Redis error (was silently returning None → wrong reason code) |
| C4 | `sdk/amaze/_core.py` | Catch `HTTPError` before `URLError` — don't retry 4xx/5xx |
| C5 | `docker/container_spawn.py` | CA mount: switched from broken bind-mount to named volume |
| S1 | `services/proxy/counters.py` | `session_id` stashed in flow.metadata; one fewer Redis round-trip per request |
| S2 | `services/proxy/_redis.py` (new) | Single shared async Redis client |
| S3 | `services/orchestrator/main.py` | `/resolve/mcp` 503 on RedisError (not bare 500) |
| S4 | `Dockerfile` + `supervisord.conf` + `entrypoint.sh` | Non-root `amaze` user (uid 1000); entrypoint chowns volumes on boot |

Nits deferred (documented): no HEALTHCHECK, `pretty_host` vs `host`,
`LLM_HOSTS` hardcoded, SSE token counter blind spot, `urllib` vs `httpx`
in register path.

Regression: all three integration tests (ST-S1.10/.11/.12) green on the
fixed stack. Bitcoin LLM chain, weather via Tavily `web_search`, NY-news
`dummy_email` denial — all working end-to-end.

---

## 2026-04-21 — Sprint S1 all integration tests green with real LLM + MCP

With `.env` populated from `aMazeControlPlane/.env` (OPENAI_API_KEY,
TAVILY_API_KEY), the three integration tests pass end-to-end on live
containers. Summary of the live runs:

| Test | Prompt | Result |
|---|---|---|
| ST-S1.10 | "search for current bitcoin price" | 200, agent-sdk → LLM → A2A agent-sdk1 → LLM → reply |
| ST-S1.11 | "search for current weather in London" | 200, real Tavily `web_search`: *sunny 12.2°C, wind ENE 12.3 mph, humidity 35%* |
| ST-S1.12 | "email me the current NEW YORK news" | Proxy 403 `tool-not-allowed` on `dummy_email`; caller sees TaskGroup error (spec-expected) |

Redis counters populated (`redis-cli --scan --pattern 'agent:*' 'session:*'`):
`agent:agent-sdk:total_llm_tokens=3208`, `agent:agent-sdk2:total_llm_tokens=1839`,
per-session MCP tool call counts, bearer TTLs all OK.

**Fixes required to get here (on top of layer 1–4 scaffold):**

| Problem | Fix |
|---|---|
| `docker compose --env-file` not passed → `OPENAI_API_KEY` blank | Always invoke compose with `--env-file .env` (compose's default looks for `.env` next to the base compose file, not repo root) |
| `demo-mcp` outbound Tavily call denied by proxy — no bearer | Drop `HTTP_PROXY` env from `demo-mcp`; attach to `amaze-egress`. Step 1 scope: proxy governs agent→MCP, not MCP→public-internet |
| `demo-mcp` TLS verify fails against real CAs | Dropped `SSL_CERT_FILE` override from `Dockerfile.mcp-base` — MCP needs the system Debian CA bundle, not our mitmproxy CA (outbound doesn't traverse our proxy) |
| agent-sdk1 inherited a "if bitcoin rewrite to dogecoin + get carol email" guard → LLM tried `dummy_email` → 403 loop | Removed the guard; agent-sdk1 is now a clean terminal LLM hop |
| LLM picked non-allowlisted MCP tools from the full advertised list | Tightened both agents' system prompts: "Use web_search tool ONLY when needed; do not call any other tool" |

**Atomic tests** (ST-S1.1 through ST-S1.9) all green except ST-S1.6 —
implementation returns `host-not-allowed` where the spec asks for
`mcp-not-allowed` on unknown servers. Noted for a post-demo triage.

**End-of-sprint commit log:**
```
57b317c  ST-S1.10/.11/.12 green with real OpenAI + Tavily
b7dea38  Sprint S1 full stack wires end-to-end
4f67394  fixup: platform boots clean; Progress.md log
bbce1d6  Sprint S1 layer 4: demo compose + Dockerfiles + SDK register update
6dff8ff  Sprint S1 layer 3: proxy addons (identity, enforcement, counters)
4cf47f0  Sprint S1 layer 2: orchestrator (register + resolve MCP)
cf21fe5  Sprint S1 layer 1: platform Dockerfile, supervisord, config YAMLs
9ef85a6  Sprint S1 plan: self-registration, 2-file config, demo-mcp, 12 tests
a919d91  Step 1 scaffold: SDK, demo agents, MCP server, proxy-only Docker isolation
```

---

## 2026-04-21 — Sprint S1 full stack reaches upstream

`docker compose up` brings the full demo stack up (amaze-platform + 3 agents
+ demo-mcp). End-to-end flow reaches api.openai.com with policy enforced
at every hop. Only blocker is a real OpenAI key (expected — infra works,
no LLM call will complete without one).

**Fixes landed on top of the layer-4 cut:**

| Problem | Fix |
|---|---|
| `docker/Dockerfile` path wrong in compose | `dockerfile: Dockerfile` |
| `[rpcinterface:supervisor]` cannot be resolved | Dropped unused supervisord IPC sections (apt's supervisor binary can't import them under python:3.12-slim) |
| Dockerfile `ENV SSL_CERT_FILE=/etc/amaze/ca/...` set BEFORE `pip install` — pip couldn't verify TLS against pypi | Moved the ENV to AFTER pip install in both agent + mcp Dockerfiles |
| `amaze-agent-net` is `internal: true` — can't publish a port to the host | Added `amaze-frontend` bridge (non-internal); `agent-sdk` joins both nets |
| `IndentationError` in `agent_sdk1.py:77` (inherited from copy) | Fixed indentation of the pre-existing `if "bitcoin"` guard |
| langchain 0.3 doesn't export `create_agent` | Bumped to langchain >=1.0 in examples/agents/requirements.txt |
| MCP and LLM calls failed with 403 invalid-bearer — agents didn't inject the bearer; openai SDK reserves `Authorization` for its own API key | 1. Renamed identity header to `X-Amaze-Bearer` (proxy side + SDK side). 2. Added an httpx.Client / AsyncClient `__init__` monkey-patch in `sdk/amaze/_core.py` that installs a request-event-hook injecting the bearer at request time. The patch runs at import, so `llm = ChatOpenAI(...)` at module-load also picks it up. |

**Proven working end-to-end:**

| Check | Result |
|---|---|
| Agent registers via `POST /register` | ✓ |
| Agent's httpx clients (langchain + openai + langchain-mcp-adapters) carry `X-Amaze-Bearer` automatically | ✓ |
| MCP handshake `POST /mcp` + `tools/list` pass the policy addon | ✓ |
| LLM POST to `api.openai.com/v1/chat/completions` reaches upstream (401 only because no real API key was set in env) | ✓ |
| Proxy denies `host-not-allowed` / `invalid-bearer` correctly | ✓ |

**Not yet exercised with real traffic** (will pass once an OPENAI_API_KEY
is set — no architectural changes needed):

- ST-S1.10 — bitcoin → LLM → A2A to agent-sdk1 → LLM → back
- ST-S1.11 — weather → A2A to agent-sdk2 → MCP web_search allow
- ST-S1.12 — NY news → A2A to agent-sdk2 → MCP dummy_email DENY

**Spec tweak still needed:** ST-S1.6 expects `mcp-not-allowed` on unknown
hosts; implementation returns `host-not-allowed` (the host just isn't
registered, so the MCP branch never runs). Either classify on hostname
pattern or adjust the spec.

---

## 2026-04-21 — Sprint S1 first boot

Platform container builds and boots clean after two small fixups:

- `docker/docker-compose.yml` — corrected build dockerfile path
  (`docker/Dockerfile` → `Dockerfile`, since the file lives at repo root).
- `docker/supervisord.conf` — dropped `[unix_http_server]`,
  `[rpcinterface:supervisor]`, `[supervisorctl]` sections; the apt
  `supervisor` package installs the binary but doesn't populate the system
  Python path that those sections import from, and we don't need
  `supervisorctl` for nodaemon mode.

Smoke-tested against the running container (no agent siblings yet):

| Check | Result |
|---|---|
| Boot: all 3 supervisord programs RUNNING | ✓ |
| `GET /health` → `{status: ok, redis: true, proxy_ca: true}` | ✓ |
| mitmproxy auto-generated CA at `/opt/mitmproxy/mitmproxy-ca-cert.pem` | ✓ |
| `POST /register {agent_id: "agent-sdk"}` → 201 `{session, bearer}` | ✓ |
| `GET /resolve/mcp/demo-mcp` (YAML-bootstrapped) | ✓ |
| `POST /register?kind=mcp` then resolve | ✓ |
| Proxy no-bearer request → 403 `invalid-bearer` | ✓ |
| Proxy with bearer + disallowed LLM model → 403 `llm-not-allowed` | ✓ |
| Proxy with bearer + unknown host → 403 `host-not-allowed` | ✓ |

---

## 2026-04-21 — Sprint S1 scaffold + code landed

Four-layer build-out across five commits:

- `a919d91` scaffold (SDK, agents, MCP server, Docker isolation)
- `9ef85a6` plan: self-registration, 2-file config, demo-mcp, 12 tests
- `cf21fe5` layer 1: Dockerfile, supervisord, config YAMLs
- `4cf47f0` layer 2: orchestrator (register + resolve MCP)
- `6dff8ff` layer 3: proxy addons (identity, enforcer, counters)
- `bbce1d6` layer 4: demo compose, agent & MCP Dockerfiles, SDK /register
