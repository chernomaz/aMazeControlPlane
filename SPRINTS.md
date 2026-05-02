# SPRINTS.md

Sprint plan for the `newAmazeControlPlane` wiring. Each sprint ships a
runnable demo and a set of system-level tests agreed with the user before
any test code is written (per CLAUDE.md §11).

---

## Sprint S1 — First wiring

**Goal.** Prove the end-to-end control-plane works: a user-started agent
container self-registers, reaches an LLM through the proxy, A2As to a peer
agent, calls an allowed MCP tool, gets denied on a non-allowed one.
Single-container platform, Redis-only state, two YAMLs, bearer-token identity,
MCP tool allowlist enforced.

### Scope

- Platform container: `redis` + `orchestrator` + `proxy (mitmproxy +
  policy-addon)` under supervisord.
- Orchestrator HTTP (passive): `POST /register`, `POST /register?kind=mcp`,
  `GET /resolve/mcp/{name}`, `GET /resolve/agent/{session}/{agent_id}`,
  `GET /health`.
- Proxy addons: `session_id` (bearer → agent_id), `policy_enforcer`
  (in-process, no separate service), `router` (resolve by Host header +
  forward), `token_counter` (Redis INCRBY from LLM usage).
- Config: `config/policies.yaml`, `config/mcp_servers.yaml`.
- Demo: three agent containers (agent-sdk, agent-sdk1, agent-sdk2) + one
  MCP container (`demo-mcp`), user-started via a small compose file.

### Explicit non-goals (deferred to later sprints)

- ExecutionGraph (Sprint 2).
- Postgres persistence / alembic migrations.
- Admin auth on `/register` (any client can claim any `agent_id` — contained
  by fail-closed policy lookup).
- Dynamic policy reload.
- React UI.
- LangSmith tracing passthrough.

---

## System tests (sign-off required before any test code is written)

Status legend: ☐ not started · ◐ in progress · ✓ green

### Atomic / wiring

| ID | Status | Name | What it exercises | Expected |
|---|---|---|---|---|
| ST-S1.1 | ☐ | Boot health | Platform container up; `redis-cli ping` OK; orchestrator `:8001` and proxy `:8080` answer `/health`; named volume `amaze-proxy-ca` contains `mitmproxy-ca-cert.pem`. | All up within 30s. |
| ST-S1.2 | ☐ | Agent register | User-started sibling calls `POST http://amaze:8001/register {agent_id: "planner"}`. | 201, body `{session_id, bearer_token}`; Redis holds `session_token:{token} → planner`. |
| ST-S1.3 | ☐ | MCP register | `POST /register?kind=mcp {name: "demo-mcp", url: "http://demo-mcp:8000/mcp/", tools: ["web_search", "dummy_email", ...]}`. | 201; `GET /resolve/mcp/demo-mcp` returns the same payload. |
| ST-S1.4 | ☐ | MCP allow | Registered `planner` (policy permits `demo-mcp/web_search`) calls the tool through the proxy. | 200 with response body; Redis counter `session:{id}:mcp:demo-mcp:web_search` == 1. |
| ST-S1.5 | ☐ | MCP deny — tool | Same agent calls `demo-mcp/delete_all` — server OK, tool not in policy. | 403, body `{reason: "tool-not-allowed", tool: "delete_all"}`; upstream **not** hit. |
| ST-S1.6 | ☐ | MCP deny — server unknown | Agent calls `evil-mcp/anything` (never registered). | 403, body `{reason: "mcp-not-allowed", server: "evil-mcp"}`. |
| ST-S1.7 | ☐ | Caller injection (spoof-proof) | Agent sends a bogus `x-amaze-caller: attacker` header alongside its bearer. | Upstream sees `x-amaze-caller: planner` (bearer's real id); spoofed value stripped. |
| ST-S1.8 | ☐ | Invalid bearer | No `Authorization` header, or `Bearer garbage`. | 403, body `{reason: "invalid-bearer"}`. |
| ST-S1.9 | ☐ | Fail closed | Two sub-cases: (a) agent registers with `agent_id` not present in `policies.yaml` → every request 403 `policy-not-found`; (b) addon raises (e.g., malformed policy loaded into memory) → 403, never 200. | Both 403. Process does not crash. |

### Integration / end-to-end

| ID | Status | Name | Driver | Expected |
|---|---|---|---|---|
| ST-S1.10 | ✓ | Bitcoin (A2A + LLM) | User POSTs to `agent-sdk`: *"search for current bitcoin price"*. `agent-sdk` → LLM → routes to `agent-sdk1` (keyword "bitcoin") via A2A → `agent-sdk1` → LLM → result cascades back. | 200 end-to-end; Redis shows LLM-token counters incremented on both `agent-sdk` and `agent-sdk1`; A2A call count incremented on `agent-sdk`. |
| ST-S1.11 | ✓ | Weather (A2A + LLM + MCP allow) | User POSTs to `agent-sdk`: *"search for current weather in London"*. `agent-sdk` → LLM → routes to `agent-sdk2` via A2A → `agent-sdk2`'s LLM calls `demo-mcp/web_search` (policy allows) → result cascades back. | 200 end-to-end; `web_search` counter on `agent-sdk2` incremented; upstream MCP call observed. |
| ST-S1.12 | ✓ | NY news (MCP tool deny) | User POSTs to `agent-sdk`: *"search for current NEW YORK news"*. Routes to `agent-sdk2`; agent-sdk2's LLM attempts `demo-mcp/dummy_email` — not in `agent-sdk2`'s allowlist. | Proxy returns 403 `tool-not-allowed`; MCP upstream **not** hit; `agent-sdk2` surfaces a "tool not permitted" error to the caller; final user response contains the error, not a successful email read. |

---

## Sprint S2 — Policy redesign + Graph enforcement + Observability

**Goal.** Replace S1 YAML policies with the unified policy model. Implement
ExecutionGraph enforcement (strict + flexible modes). Add audit logs (Redis
Streams), time-based metrics (RedisTimeSeries), and OTel traces (Jaeger).
No UI — backend and infrastructure only.

### Key design decisions

- **Policy** is one entity (no separate graph entity). Contains resource
  constraints + behavioral mode (strict/flexible) + optional graph steps.
- **Graph nodes**: tools and agents only — no LLM nodes. LLM calls are
  implicit; enforcement stays in the policy layer (allowed providers, token
  budgets).
- **Modes**: `strict` = exact step order enforced; `flexible` = allowed
  tools/agents in any order. `max_loops` per step is strict-mode only.
- **Turn = session**: one session = one turn. Per-turn counters are keyed by
  session ID and never reset within a session.
- **Violation behavior**: `on_violation: block | allow` — user configures
  per policy. An alert is **always** written to the audit log on any
  violation regardless of mode. `block` additionally calls `deny()`;
  `allow` lets the request pass through.
- **Redis** split into `redis/redis-stack` separate container (provides
  TimeSeries + Streams modules). `_redis.py` URL → `redis://amaze-redis:6379`.
  `amaze-redis-data` volume moves to the redis-stack container.
- **Audit logs**: Redis Streams (`XADD`) — one record per proxied call,
  includes `trace_id` + `span_id`. Denied requests logged with
  `denied: true, denial_reason`. Output field always populated because
  streaming is disabled at the proxy layer.
- **Metrics**: RedisTimeSeries — replaces plain `INCR` counters; supports
  time-window rate limit checks (pre-check in `request` hook; one-request
  lag on boundary crossing is expected and acceptable).
- **Traces**: OTel spans → Jaeger all-in-one (bundled in platform container
  under supervisord, port 16686 UI + 4317 OTLP). Dedicated `Tracer` addon
  owns span open/close; all other addons add attributes via
  `flow.metadata["otel_span"]`.
- **Streaming**: proxy injects `"stream": false` into every LLM request body
  before forwarding. Ensures complete response bodies for audit records,
  token counting, and rate limit enforcement.

### Roles

| Symbol | Role |
|---|---|
| Arch | Architect |
| Dev | Developer |
| QA | Quality assurance |
| GUI | GUI Developer *(not in S2)* |

### Tasks

| ID | Task | Role | Depends on |
|---|---|---|---|
| T2-1 | Split Redis: `redis/redis-stack` as separate container; remove `redis-server` from Dockerfile; update `_redis.py` URL to `redis://amaze-redis:6379`; migrate `amaze-redis-data` volume to redis-stack container | Dev | — | ✓ |
| T2-2 | Design unified policy YAML schema (per-turn limits, rate limits, allowed providers, strict/flexible mode, graph steps; fail loudly on unknown fields) | Arch | — | ✓ |
| T2-3 | Design GraphEnforcer logic (strict/flexible, step state in Redis, loop counters, violation modes, 24h TTL on graph state keys) | Arch | T2-2 | ✓ |
| T2-3b | Design violation flow control: alert always written to audit log on any violation; `on_violation: block` additionally calls `deny()`; `on_violation: allow` passes through without `deny()` | Arch | T2-3 | ✓ |
| T2-4 | Design OTel trace schema (span attributes per call type, `flow.metadata["otel_span"]` convention, dedicated Tracer addon owns open/close, Jaeger wiring) | Arch | — | ✓ |
| T2-5 | Design audit log schema (Stream key structure, record fields, `trace_id`/`span_id` link, `denied`/`denial_reason` fields) | Arch | — | ✓ |
| T2-6 | Add Jaeger all-in-one to platform container (Dockerfile + supervisord, ports 16686 + 4317, Badger persistence volume) | Dev | T2-1 | ✓ |
| T2-7 | Implement new policy loader; update `enforcer.py`; migrate `config/policies.yaml` for all three test agents to new schema | Dev | T2-2, T2-1 | ✓ |
| T2-8 | Implement GraphEnforcer proxy addon (strict + flexible + alert mode + 24h TTL on graph keys) | Dev | T2-3, T2-3b, T2-7, T2-1 | ✓ |
| T2-9 | Migrate `counters.py` to RedisTimeSeries; implement rate-limit pre-checks in `request` hook | Dev | T2-7, T2-1 | ✓ |
| T2-10 | Implement audit log writer in proxy (XADD on every call including denials; empty output for streaming) | Dev | T2-5, T2-1 | ✓ |
| T2-11 | Add dedicated `Tracer` addon + OTel instrumentation on all addons; wire OTLP exporter to Jaeger | Dev | T2-4, T2-6 | ✓ |
| T2-11b | Add `StreamBlocker` addon: inject `"stream": false` into LLM request bodies before forwarding | Dev | T2-1 | ✓ |
| T2-12 | System tests | QA | all above | ✓ |
| T2-CR | Code-review pass; fix 1 blocking + 6 should-fix findings | Dev | T2-12 | ✓ |

### Parallel phases

```
Phase 1 (parallel):   T2-1    T2-2    T2-4    T2-5
                        ↓       ↓
Phase 2a (parallel):  T2-6   T2-7   T2-3→T2-3b
                               ↓          ↓
Phase 2b (parallel):         T2-9    T2-8    T2-10    T2-11    T2-11b
                                               ↓
Phase 3:                                     T2-12
```

### Unified policy YAML shape

```yaml
name: researcher-policy

# Per-turn resource limits (turn = session: counters keyed by session ID)
max_tokens_per_turn: 10000
max_tool_calls_per_turn: 20
max_agent_calls_per_turn: 5
allowed_llm_providers: [openai, anthropic]

# Time-based token budgets (RedisTimeSeries — pre-checked on request)
token_rate_limits:
  - window: 10m
    max_tokens: 2000
  - window: 1h
    max_tokens: 10000

# Enforcement — alert always written to audit log; setting controls blocking only
on_budget_exceeded: block     # block | allow
on_violation: block           # block | allow

# Behavioral mode
mode: strict                  # strict | flexible

# flexible: set of allowed tools/agents (any order, no max_loops)
allowed_tools: [web_search, dummy_email]
allowed_agents: [agent-sdk1]

# strict: ordered graph (tools/agents only — no LLM nodes)
# max_loops is strict-mode only, defined per step
graph:
  start_step: 1
  steps:
    - step_id: 1
      call_type: tool
      callee_id: web_search
      max_loops: 2
      next_steps: [2]
    - step_id: 2
      call_type: tool
      callee_id: dummy_email
      max_loops: 1
      next_steps: []
```

---

## System tests S2 (sign-off required before any test code is written)

Status legend: ☐ not started · ◐ in progress · ✓ green

Drivers: `agent_sdk.py`, `agent_sdk1.py`, `agent_sdk2.py` — used as-is, no
modifications.

**Every test verifies three things:**
1. **Functional** — correct HTTP status + response body
2. **Audit log** — Redis Stream contains the correct call sequence with right fields
3. **Trace** — matching span visible in Jaeger; `trace_id` in audit record matches Jaeger

| ID | Status | Name | What it exercises | Expected |
|---|---|---|---|---|
| ST-S2.1 | ◐ | Strict — correct sequence | `test-strict` agent; web_search then dummy_email in order | 200; audit: `web_search` before `dummy_email`, both with `trace_id`; Jaeger: 2 tool spans in order |
| ST-S2.2 | ◐ | Strict — wrong order | `test-strict`; dummy_email attempted before web_search | 403 `graph_violation`; audit: `denied:true`, `denial_reason:graph_violation` |
| ST-S2.3 | ◐ | Flexible — any order passes | `test-flexible`; tools called in reverse order | 200; audit: both tools recorded in actual order |
| ST-S2.4 | ◐ | Flexible — unlisted tool | `test-flexible`; calls tool not in `allowed_tools` | 403 `tool-not-allowed`; audit: `denied:true`, `denial_reason:tool-not-allowed` |
| ST-S2.5 | ◐ | Allow mode — violation passes | `test-strict-allow`; wrong order with `on_violation:allow` | 200; audit: `denied:false`, `alert` field has violation data |
| ST-S2.6 | ◐ | Loop limit | `test-strict-loop`; max_loops:1, call tool twice | Second call → 429 `edge_loop_exceeded`; audit: second record `denied:true` |
| ST-S2.7 | ◐ | Token budget per turn | `test-low-tokens` (limit 50); counter pre-seeded to 50 | 403 `budget_exceeded`; audit: `denied:true`, `kind:llm` |
| ST-S2.8 | ◐ | Tool call limit per turn | `test-low-tools` (limit 1); call two tools | Second tool → 403 `tool-limit-exceeded`; audit: second `denied:true` |
| ST-S2.9 | ◐ | Rate limit window | `test-rate-limit` (1m/50tok); TS pre-seeded to 50 | 403 `rate-limit-exceeded`; audit: `denied:true` |
| ST-S2.10 | ◐ | A2A trace propagation | `test-a2a-caller` → `test-a2a-callee`; LLM call from callee | 200; both audit records share same `trace_id`; `trace_context:{sid}` in Redis |
| ST-S2.11 | ◐ | Audit log completeness | Re-run ST-S2.1; inspect stream | All required fields present and non-empty for allowed calls |
| ST-S2.12 | ◐ | RedisTimeSeries metrics | LLM call via `test-a2a-callee`; query TS key | `TS.RANGE ts:test-a2a-callee:llm_tokens` returns 25 tokens (mock-llm value) |
| ST-S2.13 | ◐ | stream:false enforced | LLM call via `test-a2a-callee`; check audit input | `audit.input` JSON contains `"stream": false` |

---

---

## Sprint S3 — Remote Agent + MCP Runtime MVP

**Date:** 2026-04-29

**Goal:** Replace Docker-DNS routing with Redis-backed endpoint registration.
Agents and MCP servers self-register their reachable `host:port` at startup.
The proxy resolves all A2A and MCP routing from Redis — no Docker network
dependency, no shared compose network. Same-host multi-port and different-host
deployments both work. The transparent proxy model (`HTTP_PROXY`) is unchanged.

**What changes:**
- Agents register `a2a_host + a2a_port` with the orchestrator on startup
- Orchestrator stores `agent:{id}:endpoint` in Redis
- New `Router` addon rewrites `flow.request.host/port` from Redis before
  mitmproxy opens the upstream connection
- `AMAZE_A2A_HOST` env var replaces `AMAZE_A2A_PUBLIC_URL`; port mappings
  made symmetric per agent

**What stays exactly the same:**
- `HTTP_PROXY` / `HTTPS_PROXY` on agents (just points to real IP via
  `AMAZE_PROXY_HOST` — already configurable)
- mitmproxy and the entire addon chain — zero changes
- LLM path — agent calls `api.openai.com`, proxy MITM's as before
- Bearer injection in SDK
- `PolicyEnforcer` host-based classification
- All existing system tests

### Roles

| Symbol | Role |
|---|---|
| Arch | Architect |
| Dev | Developer |
| QA | Quality assurance |
| DevOps | Infrastructure / compose |

### Tasks

| ID | Status | Name | Role | Files | Depends on |
|---|---|---|---|---|---|
| S3-T1 | ✓ | Architecture doc | Arch | `docs/remote-routing-architecture.md` (new) | — |
| S3-T2 | ✓ | Registration API — accept `a2a_host + a2a_port`; store `agent:{id}:endpoint` in Redis; add `GET /resolve/agent/{id}` | Dev | `services/orchestrator/main.py` | S3-T1 |
| S3-T3 | ✓ | SDK — add `AMAZE_A2A_HOST` to Config; send `a2a_host + a2a_port` in `POST /register` | Dev | `sdk/amaze/_core.py` | S3-T1 |
| S3-T4 | ✓ | Router addon — look up `agent:{id}:endpoint` / `mcp:{name}` from Redis; rewrite `flow.request.host + port`; runs last in addon chain | Dev | `services/proxy/router.py` (new) `services/proxy/main.py` | S3-T2 |
| S3-T5 | ✓ | Compose + multi-host simulation — `AMAZE_A2A_HOST` per agent; fix symmetric port mappings; separate compose projects for control / agent / MCP | DevOps | `examples/compose.yml` `docker/compose-agent-host.yml` (new) `docker/compose-mcp-host.yml` (new) `scripts/demo_remote_runtime.sh` (new) | S3-T1 |
| S3-T6 | ✓ | System integration tests | QA | `tests/test_s3.py` (new) `tests/conftest.py` (new) `config/policies.yaml` (S3 test agents) | all above |
| S3-T7 | ✓ | Sprint demo | All | `reports/demo_remote_runtime.md` (new) | S3-T6 |

### Parallel phases

```
Phase 0 ── S3-T1
           (Arch — endpoint registration contract; gates all Dev)
           │
           ├─────────────────────────────────┐
Phase 1    │                                 │
(parallel) S3-T2        S3-T3      S3-T5
           orchestr.    sdk        compose
           main.py      _core.py   examples/
                                   docker/
           └──────────────┬────────────────-─┘
                          │
Phase 2                S3-T4
                    router.py (new)
                    proxy/main.py
                          │
Phase 3                S3-T6
                    system tests
                          │
Phase 4                S3-T7
                        demo
```

**File overlap check — Phase 1 (all clear):**

| Task | Files | Overlap |
|---|---|---|
| S3-T2 | `services/orchestrator/main.py` | ✓ |
| S3-T3 | `sdk/amaze/_core.py` | ✓ |
| S3-T4 | `services/proxy/router.py` (new), `services/proxy/main.py` | ✓ |
| S3-T5 | `examples/compose.yml`, `docker/compose-*.yml` (new), `scripts/` (new) | ✓ |

**S3-T4 is sequential after Phase 1** — Router addon reads `agent:{id}:endpoint`
keys written by S3-T2 and the `AMAZE_A2A_HOST` sent by the reworked SDK (S3-T3).
Both must be complete before S3-T4 can be verified end-to-end.

### How routing works after S3

```
Before (Docker DNS):
  agent-sdk → POST http://agent-sdk1/a2a/tasks  (via HTTP_PROXY)
  mitmproxy → DNS resolves agent-sdk1 → 172.18.0.5 (Docker bridge)
  forwards  → 172.18.0.5:9002

After (Redis lookup):
  agent-sdk → POST http://agent-sdk1/a2a/tasks  (via HTTP_PROXY — unchanged)
  PolicyEnforcer: host="agent-sdk1" ∈ allowed_agents → permit  (unchanged)
  Router addon: GET agent:agent-sdk1:endpoint → "http://host.docker.internal:9003"
  flow.request.host = "host.docker.internal"
  flow.request.port = 9003
  forwards  → host.docker.internal:9003  (no Docker DNS needed)
```

### Compose changes (S3-T5)

```yaml
# examples/compose.yml — per agent

agent-sdk:
  environment:
    AMAZE_A2A_HOST: ${AMAZE_PROXY_HOST:-host.docker.internal}  # new
    AMAZE_A2A_PORT: "9002"                                      # was absent
    # AMAZE_A2A_PUBLIC_URL removed
  ports:
    - "9002:9002"   # unchanged

agent-sdk1:
  environment:
    AMAZE_A2A_HOST: ${AMAZE_PROXY_HOST:-host.docker.internal}
    AMAZE_A2A_PORT: "9003"                                      # was absent
  ports:
    - "9003:9003"   # was 9003:9002 — now symmetric

agent-sdk2:
  environment:
    AMAZE_A2A_HOST: ${AMAZE_PROXY_HOST:-host.docker.internal}
    AMAZE_A2A_PORT: "9004"                                      # was absent
  ports:
    - "9004:9004"   # was 9004:9002 — now symmetric
```

### Remote agent (different host, no compose)

```bash
AMAZE_PROXY_HOST=203.0.113.10   # platform's real IP
AMAZE_A2A_HOST=198.51.100.20    # this agent's own reachable IP
AMAZE_A2A_PORT=9002
HTTP_PROXY=http://203.0.113.10:8080
HTTPS_PROXY=http://203.0.113.10:8080
```

Registers → `agent:{id}:endpoint = http://198.51.100.20:9002` in Redis.
Proxy routes inbound A2A to it. No Docker network. No DNS.

---

### System tests S3 (signed off — do not write test code without this)

Status legend: ☐ not started · ◐ in progress · ✓ green

**Every test verifies:**
1. Correct HTTP status + response body
2. Audit record written to Redis Stream with correct fields
3. Relevant Redis counters updated

#### Atomic

| ID | Status | Name | Steps | Expected |
|---|---|---|---|---|
| ST-S3.1 | ✓ | Remote registration | Start control plane; start agent + MCP from separate compose projects | Both marked ONLINE in Redis; `/health` confirms |
| ST-S3.2 | ✓ | Agent → MCP allowed | Agent calls `demo-mcp.web_search` via proxy | 200; audit written; tool counter incremented |
| ST-S3.3 | ✓ | Agent → MCP denied tool | Agent calls `demo-mcp.dummy_email` (not in policy) | 403 `tool-not-allowed`; audit written; upstream not hit |
| ST-S3.4 | ✓ | Agent → Agent allowed | `agent-sdk` calls `agent-sdk1` | 200; A2A audit written |
| ST-S3.5 | ✓ | Agent → Agent denied | `agent-sdk1` calls `agent-sdk2` (not in policy) | 403 `not-allowed`; audit written |
| ST-S3.6 | ✓ | Agent → LLM allowed | Agent calls `openai/gpt-4.1-mini` via proxy | 200; token counter updated; audit written |
| ST-S3.7 | ✓ | Agent → LLM denied | Agent calls disallowed model | 403 `llm-not-allowed`; audit written |

#### Integration / end-to-end

| ID | Status | Name | Driver | Expected |
|---|---|---|---|---|
| ST-S3.8 | ✓ | Bitcoin (A2A + LLM) | User POSTs to `agent-sdk`: *"search for current bitcoin price"*. `agent-sdk` → LLM → routes to `agent-sdk1` (keyword "bitcoin") via A2A → `agent-sdk1` → LLM → result cascades back. | 200 end-to-end; Redis shows LLM-token counters incremented on both `agent-sdk` and `agent-sdk1`; A2A call count incremented on `agent-sdk`. |
| ST-S3.9 | ✓ | Weather (A2A + LLM + MCP allow) | User POSTs to `agent-sdk`: *"search for current weather in London"*. `agent-sdk` → LLM → routes to `agent-sdk2` via A2A → `agent-sdk2`'s LLM calls `demo-mcp/web_search` (policy allows) → result cascades back. | 200 end-to-end; `web_search` counter on `agent-sdk2` incremented; upstream MCP call observed. |
| ST-S3.10 | ✓ | NY news (MCP tool deny) | User POSTs to `agent-sdk`: *"search for current NEW YORK news"*. `agent-sdk`'s LLM attempts `demo-mcp/dummy_email` directly — not in policy. | Proxy returns 403 `tool-not-allowed`; MCP upstream **not** hit; error propagates back; final user response contains the error. |

---

## Sprint S4 — GUI implementation (real, replaces mock)

**Date:** 2026-04-30

**Goal.** Replace `services/ui_mock/index.html` with a real React GUI that
lights up with live data. Fill the missing read/write API gaps the GUI needs
(agents/mcp/llms list, audit query, approval, policy CRUD, send-message,
trace detail, stats, export). The output is a runnable app driving demos
end-to-end from the browser: approve a pending MCP server → write a strict
policy → send a message → watch the trace appear → drill in.

**Tool-advertisement filter** (TODO.md #1) stays parked in `TODO.md` for a
later sprint — out of scope for S4.

**The mock at `services/ui_mock/index.html` (1 806 lines) is the design
source of truth.** Each UI page is ported by reading the corresponding
section of the mock; locked decisions from two prior feedback rounds are
listed below and not revisited.

### Scope

- New React app at `services/ui/` (same repo, `Dockerfile.ui` builds + serves)
- Stack: **React 19 + Vite 5 + TypeScript + Tailwind + Radix + XYFlow** —
  build shell, Radix/CVA primitives, `apiFetch`, ReactFlow integration
  transplanted from `/home/ubuntu/data/cloude/aMaze/services/ui/` (skip its
  old page layouts — mock supersedes them)
- Orchestrator gains read endpoints (`/agents`, `/mcp_servers`, `/llms`,
  `/traces`, `/traces/{id}`, `/agents/{id}/stats`, `/alerts`) and write
  endpoints (`/agents/{id}/approve|reject`, `/mcp_servers/{name}/approve|reject`,
  `POST /llms`, `POST /mcp_servers`, `PUT /policy/{id}`,
  `POST /agents/{id}/messages`, `GET /export`)
- **Policy storage moves to Redis primary.** `config/policies.yaml` becomes
  bootstrap-only: read once when Redis key `policy:{agent_id}` is absent;
  ignored thereafter. UI edits write Redis directly. Enforcer refetches
  per-request (no boot-time-only cache; restart no longer required).
- LiteLLM as the LLM-provider abstraction (`config/litellm.yaml` emitted
  by `POST /llms`)

### Locked design decisions (from `services/ui_mock/index.html`)

- Sidebar: LLM Providers · MCP Servers · Agents · Traces · Alerts (no Stats tab)
- Time-range picker on agent dashboard header only, NOT sidebar
- Self-registration; UI only approves (manual `Add LLM` and `Add MCP Server`
  modals are the exception)
- Policy + Execution Graph = ONE combined tab (graph editor inline within
  strict mode)
- Agent right-panel conditional: pending → approval card; approved-no-policy →
  policy editor opens directly; approved-with-policy → dashboard view
- Trace detail = full-page overlay with ← Back, NOT modal
- Alerts tab donut interactive (slice click filters traces below)
- Agent dashboard donuts: Tokens/Call · Tool Calls · Agent→Agent · Alert
  Reasons (clickable to filter Traces / pre-filter Alerts)
- Export modal: date range + agent filter + content checkboxes → ZIP

### Roles

| Symbol | Role |
|---|---|
| Arch | Architect |
| Dev | Developer |
| GUI | GUI Developer |
| QA | Quality assurance |

### Tasks

Status legend: ☐ not started · ◐ in progress · ✓ green

#### Phase 1 — Foundation

| ID | Status | Task | Role | Files | Depends on |
|---|---|---|---|---|---|
| S4-T1.0 | ✓ | **System-test list sign-off** (gate per CLAUDE.md §11) | All | this file | — |
| S4-T1.1 | ✓ | UI service skeleton — port build shell, Radix primitives, `apiFetch`, lib utils; React Router with placeholder pages for the 5 locked tabs; Tailwind dark theme matching mock | GUI | `services/ui/` (new): `package.json`, `vite.config.ts`, `tsconfig.json`, `tailwind.config.ts`, `src/{App.tsx,main.tsx,index.css,components/ui/*,api/client.ts,lib/utils.ts,pages/*}` | T1.0 |
| S4-T1.2 | ✓ | Orchestrator read endpoints: `GET /agents`, `GET /mcp_servers`, `GET /llms`, `GET /traces?agent=&limit=&offset=` | Dev | `services/orchestrator/routers/{agents,mcp,llms,traces}.py` (new), `services/orchestrator/main.py` (mount routers) | T1.0, T1.3 |
| S4-T1.3 | ✓ | Audit query helpers — `XRANGE`/`XREVRANGE` wrappers with cursor + agent + denied-only filter; trace summary projection (one row per `trace_id`) | Dev | `services/orchestrator/audit_query.py` (new) | T1.0 |
| S4-T1.4 | ✓ | UI Dockerfile + compose wiring — `Dockerfile.ui` (Node build → static serve), expose on host port; orchestrator dev proxy | DevOps | `Dockerfile.ui` (new), `docker/docker-compose.yml`, `services/ui/vite.config.ts` | T1.1 |

#### Phase 2 — Approvals + policy persistence (Redis primary)

| ID | Status | Task | Role | Files | Depends on |
|---|---|---|---|---|---|
| S4-T2.1 | ✓ | Approve/reject endpoints — `POST /agents/{id}/approve`, `/agents/{id}/reject`, `/mcp_servers/{name}/approve`, `/mcp_servers/{name}/reject`. Sets `agent:{id}:approved` / `mcp:{name}:approved` in Redis | Dev | `services/orchestrator/routers/agents.py`, `services/orchestrator/routers/mcp.py` | T1.2 |
| S4-T2.2 | ✓ | **Policy storage — Redis primary.** `policy:{agent_id}` JSON keys; YAML bootstrap reads once on first boot when key absent (idempotent, never overwrites Redis); `enforcer.py` refetches per-request (no boot-time cache); `GET /policy/{id}`, `PUT /policy/{id}` | Dev | `services/proxy/policy_store.py` (new), `services/proxy/policy.py`, `services/proxy/enforcer.py`, `services/orchestrator/routers/policy.py` (new) | T1.2 |
| S4-T2.3 | ✓ | Add LLM endpoint — `POST /llms` accepts provider/model/base_url/api_key_ref; appends LiteLLM YAML model_list block to `config/litellm.yaml` | Dev | `services/orchestrator/routers/llms.py`, `config/litellm.yaml` (new) | T1.2 |
| S4-T2.4 | ✓ | Add MCP Server endpoint — `POST /mcp_servers` (manual registration, validates URL + tools[]) | Dev | `services/orchestrator/routers/mcp.py` | T1.2 |
| S4-T2.5 | ✓ | UI Approve flows — pending agent/server cards with Approve/Reject buttons, toast on success, row drops out of pending list | GUI | `services/ui/src/pages/{Agents,McpServers}.tsx`, `services/ui/src/api/{agents,mcp}.ts` (new) | T1.1, T2.1 |
| S4-T2.6 | ✓ | UI Add LLM modal — provider/model/api-key/base-url form; LiteLLM YAML preview pane | GUI | `services/ui/src/pages/Llms.tsx`, `services/ui/src/components/AddLlmModal.tsx` | T1.1, T2.3 |
| S4-T2.7 | ✓ | UI Add MCP Server modal — name/url/tools (chip input) | GUI | `services/ui/src/components/AddMcpModal.tsx` | T1.1, T2.4 |

#### Phase 3 — Policy editor + send-message + trace detail

| ID | Status | Task | Role | Files | Depends on |
|---|---|---|---|---|---|
| S4-T3.1 | ✓ | UI policy editor — combined Policy + Graph tab; strict toggles + inline graph editor; flexible checklists; per-turn limits + rate-limit table; saves via `PUT /policy/{id}` | GUI | `services/ui/src/pages/AgentPolicy.tsx`, `services/ui/src/api/policy.ts` (new) | T2.2 |
| S4-T3.2 | ✓ | UI execution-graph canvas — XYFlow custom StepNode, drag-to-connect, finish-node validation, save serializes to policy.graph schema | GUI | `services/ui/src/components/{GraphEditor,StepNode}.tsx` | T1.1 |
| S4-T3.3 | ✓ | Send-message endpoint — `POST /agents/{id}/messages` proxies to agent's A2A endpoint (resolved from `agent:{id}:endpoint`), returns task ID + final response | Dev | `services/orchestrator/routers/agents.py` | T2.1 |
| S4-T3.4 | ✓ | Trace detail endpoint — `GET /traces/{trace_id}` assembles 3-col summary, `sequence_steps[]`, `edges[]`, `violations_list[]` from audit-stream entries grouped by `trace_id`; optionally enriched with Jaeger spans | Dev | `services/orchestrator/trace_detail.py` (new), `services/orchestrator/routers/traces.py` | T1.2, T1.3 |
| S4-T3.5 | ✓ | UI Trace detail page — full-page overlay with ← Back; SVG sequence diagram (swim lanes, click-to-highlight edges), 15-col Execution Edges table, Violations section | GUI | `services/ui/src/pages/TraceDetail.tsx`, `services/ui/src/components/SequenceDiagram.tsx` | T3.4 |
| S4-T3.6 | ✓ | System tests — write the suite signed off in T1.0 | QA | `tests/test_s4.py` (new), `tests/conftest.py` | T2.*, T3.1–T3.5 |

#### Phase 4 — Stats backend

| ID | Status | Task | Role | Files | Depends on |
|---|---|---|---|---|---|
| S4-T4.1 | ✓ | RTS aggregation utilities — `TS.MRANGE`-based helpers for calls/time, tokens/call distribution, tool counts, A2A counts, alert counts grouped by `denial_reason` | Dev | `services/orchestrator/stats.py` (new) | T1.3 |
| S4-T4.2 | ✓ | Per-agent stats endpoint — `GET /agents/{id}/stats?range=1h\|24h\|7d&metric=calls\|latency\|tokens\|tools\|a2a\|alerts`; shape matches mock's `DASH_DATA` | Dev | `services/orchestrator/routers/agents.py` | T4.1 |
| S4-T4.3 | ✓ | Alerts grouped-by-reason endpoint — `GET /alerts?agent=&range=&groupBy=reason` | Dev | `services/orchestrator/routers/alerts.py` (new) | T4.1 |

#### Phase 5 — Charts + alerts UI

| ID | Status | Task | Role | Files | Depends on |
|---|---|---|---|---|---|
| S4-T5.1 | ✓ | UI agent dashboard — 5 KPI cards (calls, unique tools, avg latency, critical alerts, policy health); time-range picker in dashboard header | GUI | `services/ui/src/pages/AgentDashboard.tsx` | T4.2 |
| S4-T5.2 | ✓ | UI line chart — calls/latency tab toggle, 24-point series (Recharts or pure SVG) | GUI | `services/ui/src/components/LineChart.tsx` | T4.2 |
| S4-T5.3 | ✓ | UI 4 donut charts — Tokens/Call · Tool Calls · A2A (clickable → Traces) · Alert Reasons (clickable → Alerts pre-filtered) | GUI | `services/ui/src/components/DonutChart.tsx` | T4.2, T4.3 |
| S4-T5.4 | ✓ | UI Alerts tab — reason pills (horizontal filter), filtered traces table; row click → trace detail | GUI | `services/ui/src/pages/Alerts.tsx` | T4.3 |

#### Phase 6 — Export + sprint demo

| ID | Status | Task | Role | Files | Depends on |
|---|---|---|---|---|---|
| S4-T6.1 | ✓ | Export endpoint — `GET /export?agent=&start=&end=&content=traces,audit`; streams ZIP with `traces.json` (Jaeger query) + `audit.csv` (audit-stream rows) | Dev | `services/orchestrator/export.py` (new), `services/orchestrator/routers/export.py` (new) | T1.2 |
| S4-T6.2 | ✓ | UI export modal — date range, agent filter, content checkboxes (OTel JSON, audit CSV, sequence HTML), triggers download | GUI | `services/ui/src/components/ExportModal.tsx` | T6.1 |
| S4-T6.3 | ✓ | System tests for write-paths + export; **/code-reviewer pass on all new/modified files**; sprint demo report | QA | `tests/test_s4_export.py` (new), `Progress.md`, `reports/demo_s4_gui.md` (new) | T6.1, T6.2 |

### Parallel phases

```
Phase 1   T1.0 (sign-off gate)
              ↓
          ├── T1.1 (UI skeleton) → T1.4  ┐
          ├── T1.3 (audit helpers) → T1.2│ all parallel
                                         ↓
Phase 2   ├── T2.1 (approvals)           ┐
          ├── T2.2 (policy Redis)        │ backend parallel
          ├── T2.3 (add LLM)             │ (router split makes this safe)
          ├── T2.4 (add MCP)             │
          ├── T2.5 (UI approve)          ┐
          ├── T2.6 (UI Add LLM modal)    │ UI parallel
          ├── T2.7 (UI Add MCP modal)    ┘
                                         ↓
Phase 3   ├── T3.1+T3.2 (UI policy editor + graph canvas, sequential same area)
          ├── T3.3 (send-message endpoint)
          ├── T3.4 → T3.5 (trace detail backend → UI)
              ↓
          T3.6 (system tests for Phases 1–3)
                                         ↓
Phase 4   T4.1 → T4.2, T4.3 (parallel)
                                         ↓
Phase 5   T5.1, T5.2, T5.3, T5.4 (all parallel)
                                         ↓
Phase 6   T6.1 → T6.2 → T6.3
```

**File overlap check (Phase 2 backend):** all four tasks touch
`services/orchestrator/`, but the router split (T1.2 already creates
`routers/{agents,mcp,llms,traces}.py`) means T2.1 (agents+mcp), T2.2 (policy),
T2.3 (llms), T2.4 (mcp) write to disjoint files — full parallel is safe.

---

## System tests S4 — proposed for sign-off

Status legend: ☐ not started · ◐ in progress · ✓ green

**Every test verifies three things:**
1. **Functional** — correct HTTP status + response body shape
2. **Audit log / Redis state** — Redis Stream / TS / KV reflects expected mutation
3. **Trace** — matching span visible in Jaeger when applicable; `trace_id` in audit record matches Jaeger

### Atomic — read endpoints

| ID | Status | Name | What it exercises | Expected |
|---|---|---|---|---|
| ST-S4.1 | ☐ | `GET /agents` | List agents with state classification | Each agent has `state ∈ {pending, approved-no-policy, approved-with-policy}`; matches Redis `agent:{id}:approved` + `policy:{id}` presence |
| ST-S4.2 | ☐ | `GET /mcp_servers` | List MCP servers + tools | Returns name, url, tools[], approved flag |
| ST-S4.3 | ☐ | `GET /llms` | List LiteLLM model_list | Returns parsed `config/litellm.yaml` model_list block |
| ST-S4.4 | ☐ | `GET /traces?agent=&limit=&offset=` | Paginated audit projection | Returns one row per `trace_id` for the agent; cursor-based pagination works; matches XRANGE result count |
| ST-S4.5 | ☐ | `GET /traces/{trace_id}` | Full trace detail assembly | Returns 3-col summary + `sequence_steps[]` + `edges[]` + `violations_list[]`; matches mock's `TRACE_DATA` shape |

### Atomic — write endpoints

| ID | Status | Name | What it exercises | Expected |
|---|---|---|---|---|
| ST-S4.6 | ☐ | `POST /agents/{id}/approve` | Set approval flag | 200; Redis `agent:{id}:approved=true`; subsequent `GET /agents` shows `state=approved-no-policy` |
| ST-S4.7 | ☐ | `POST /mcp_servers/{name}/approve` | Same for MCP | 200; Redis `mcp:{name}:approved=true` |
| ST-S4.8 | ☐ | `POST /llms` (LiteLLM emit) | Append model_list block to YAML | 200; `GET /llms` returns the new entry; YAML on disk has new block |
| ST-S4.9 | ☐ | `POST /mcp_servers` (manual) | Manual registration | 201; Redis `mcp:{name}` populated; visible in `GET /mcp_servers` |
| ST-S4.10 | ☐ | `PUT /policy/{id}` (live enforcement) | Save policy → next call uses it without restart | 200; Redis `policy:{id}` updated; subsequent agent call enforces new rules (e.g. flip `mode: strict→flexible` and an unordered call now passes) |
| ST-S4.11 | ☐ | `GET /policy/{id}` | Read current policy | Returns Redis-stored value, not YAML if both differ |
| ST-S4.12 | ☐ | `POST /agents/{id}/messages` (send-message) | Proxy demo message to agent | 200; agent's A2A endpoint hit; response body returned; audit row created with the bearer's session_id |

### Atomic — stats

| ID | Status | Name | What it exercises | Expected |
|---|---|---|---|---|
| ST-S4.13 | ☐ | `GET /agents/{id}/stats?range=1h&metric=calls` | Time-bucketed series | Returns 24-point array (≤1h hourly); matches `TS.RANGE` aggregation |
| ST-S4.14 | ☐ | `GET /alerts?range=24h&groupBy=reason` | Alert distribution | Returns `{graph_violation: N, tool-not-allowed: N, …}`; sums match `ts:{*}:denials` total |

### Integration / end-to-end (matches sprint demo)

| ID | Status | Name | Driver | Expected |
|---|---|---|---|---|
| ST-S4.15 | ☐ | Approval flow | Pending MCP server → UI Approve button → demo agent calls its tool | Approval persisted in Redis; subsequent tool call passes proxy (was failing pre-approval if approval-gated, otherwise: server visible in GUI as approved) |
| ST-S4.16 | ☐ | Policy editor live update | Save new strict policy via UI → next message uses new graph (no restart) | New policy in Redis; first call out-of-graph → 403 `graph_violation`; first call in-graph → 200 |
| ST-S4.17 | ☐ | Send-message → Trace detail | UI Send Message "search for current bitcoin price" → trace_id returned → click row in Traces tab → Trace detail page renders sequence + edges | Full demo path: 200 response, trace_id present, Trace detail endpoint returns full assembled record, sequence diagram + edges table render with non-empty data |
| ST-S4.18 | ☐ | Alerts donut → filter | Trigger violation ("email me NY news"), Alerts tab donut updates, slice click filters traces table below | Alert grouped under `tool-not-allowed`; click filters table to that reason; row click opens Trace detail |
| ST-S4.19 | ☐ | Export round-trip | UI export modal → range = 1h, agent = research-agent, content = traces+audit → ZIP download | ZIP contains `traces.json` (valid Jaeger format) + `audit.csv` (correct rows for time/agent filter); both non-empty |

---

## Progress

See `Progress.md` (running log, updated as work lands).

---

## Scrum rules (recap from CLAUDE.md §11)

1. Every sprint ends with a runnable demo.
2. At least one system test per sprint.
3. Test list agreed with user *before* implementation starts.
4. No skipping iterations — ship a smaller slice rather than nothing.
5. `/code-reviewer` pass on all new/modified files at sprint end.
