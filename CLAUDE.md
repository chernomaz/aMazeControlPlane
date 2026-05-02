# aMaze Control Plane

Runtime enforcement layer for agent systems. Proxy + orchestrator + registry
in a two-container setup with YAML-driven config, full observability, and
graph-based execution policy enforcement.

---

## 1. Project Overview

Enforces:

- **A2A (Agent-to-Agent)** — who can talk to whom.
- **MCP (Model Context Protocol)** — which tools/resources an agent may call,
  and in what order.
- **LLM** — which providers/models/token budgets an agent is allowed.
- **ExecutionGraph** — strict or flexible ordering of tool/agent calls per
  session, with per-step loop limits.

All outbound traffic from agent containers is forced through a single proxy
(mitmproxy) on a Docker network with no direct internet route. The proxy runs
all policy logic in-process via addons — there is no separate policy service.
Policies are YAML, loaded at boot. Live state (session counters, bearer
tokens, graph step pointers) lives in Redis. Every proxied call is recorded
to an audit log (Redis Streams) and emitted as an OTel trace (Jaeger).

---

## 2. Core Principles

- All agent traffic MUST go through the proxy.
- No direct network access from agent runtimes.
- Proxy-only egress is mandatory — enforced by `internal: true` Docker network.
- Policy enforcement is real-time and **fails closed** — if the addon raises,
  the request is denied.
- Strict separation of concerns: A2A ≠ MCP ≠ LLM ≠ Graph.
- Deterministic rules over "smart" heuristics.
- LLM calls are never modelled in the execution graph — they are implicit and
  enforced only by the policy layer (provider allowlist, token budgets).
- Streaming LLM responses are disabled at the proxy (`stream: false` injected)
  to guarantee complete audit records and accurate token counting.
- Redis is the permanent persistence layer (no Postgres). AOF enabled.

---

## 3. Scope

- **A2A enforcement** — allow/deny between agents via `allowed_agents`.
- **MCP enforcement** — allow/deny per server and per tool.
- **LLM enforcement** — allow/deny per provider + model; token budgets.
- **ExecutionGraph enforcement** — strict (ordered steps) or flexible (allowed
  set, any order); per-step loop limits (strict only); fail-closed or alert.
- **Per-turn limits** — `max_tokens_per_turn`, `max_tool_calls_per_turn`,
  `max_agent_calls_per_turn`. **Turn = one user message + the agent's full
  response cycle.** Counters reset at the start of every turn (orchestrator's
  `POST /agents/{id}/messages` clears `session:{sid}:total_*` and
  `graph:{sid}:*` before forwarding the prompt). Earlier sprints used
  "turn = session lifetime" — changed in S4 so the GUI's click-to-message
  flow gives each message a fresh turn.
- **Time-based rate limits** — token budgets per time window (e.g. 10 min,
  1 hour) via RedisTimeSeries.
- **Audit log** — Redis Streams, one record per proxied call, includes
  `trace_id` + `span_id`, input/output, denial reason.
- **Metrics** — RedisTimeSeries for token usage, call counts, denial counts.
- **Distributed traces** — OTel spans → Jaeger (bundled in platform container).

### Out of scope (do not implement)

- UI/dashboard (Sprint 3).
- Dynamic policy reload (later sprint; restart required to reload config).
- Postgres persistence — Redis + YAML is the permanent stack.
- Intention drift detection.
- Prompt analysis / AI guardrails.
- Trust scoring.
- Testing framework.
- Multi-agent orchestration logic.
- LangChain-specific instrumentation.
- LLM nodes in the execution graph.

---

## 4. Architecture

```
Agent container (isolated net, HTTP_PROXY forced)
    ↓
Proxy (mitmproxy + addons: session_id, policy_enforcer, graph_enforcer,
       stream_blocker, tracer, router, counters, audit_log)
    ↓
Upstream:
   - Peer agent containers (A2A)
   - MCP servers (tool calls)
   - LLM providers (openai, anthropic, ...)
```

Two containers (supervisord inside platform container):

| Process | Container | Port | Role |
|---|---|---|---|
| redis-stack | amaze-redis | 6379 | session state, counters, streams, timeseries |
| orchestrator | amaze-platform | 8001 | session lifecycle, bearer token issuance |
| proxy | amaze-platform | 8080 | mitmproxy + all enforcement addons |
| jaeger | amaze-platform | 16686 / 4317 | OTel trace storage + UI |

### Proxy addon chain (in order)

1. `session_id` — resolve bearer → agent_id; deny unknown tokens
2. `policy_enforcer` — A2A / MCP / LLM allowlist checks
3. `graph_enforcer` — step ordering, loop limits, alert/block mode
4. `stream_blocker` — inject `"stream": false` into LLM request bodies
5. `tracer` — OTel span open (request) / close (response); owns span lifecycle
6. `router` — resolve host → upstream URL; forward
7. `counters` — RedisTimeSeries token + call metrics
8. `audit_log` — XADD to Redis Stream; one record per call

---

## 5. Policy Model

One `policy` entity per agent. Stored in `config/policies.yaml`.

```yaml
name: my-policy

# Per-turn resource limits — turn = one user message + agent's response cycle.
# Counters reset at the start of every send-message call (S4).
max_tokens_per_turn: 10000
max_tool_calls_per_turn: 20
max_agent_calls_per_turn: 5
allowed_llm_providers: [openai, anthropic]

# Time-based token budgets (RedisTimeSeries pre-check on request)
token_rate_limits:
  - window: 10m
    max_tokens: 2000
  - window: 1h
    max_tokens: 10000

# Enforcement — alert always written to audit log; setting controls blocking only
on_budget_exceeded: block     # block | allow
on_violation: block           # block | allow

# Behavioural mode
mode: strict                  # strict | flexible

# flexible: set of allowed tools/agents (any order, no loop limits)
allowed_tools: [web_search, dummy_email]
allowed_agents: [agent-sdk1]

# strict: ordered graph (tools/agents only — NO LLM nodes)
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

Key rules:
- LLM calls are **never** modelled as graph steps.
- `max_loops` only applies in `strict` mode.
- Alert is **always** written to the audit log on any violation, regardless
  of mode. `on_violation`/`on_budget_exceeded` only controls blocking:
  `block` = deny + alert; `allow` = pass + alert. `deny()` is NOT called
  in `allow` mode.
- Rate limit pre-check happens in the `request` hook; one-request lag on
  boundary crossing is expected and acceptable.

---

## 6. Observability

### Audit log (Redis Streams)

Key: `audit:{agent_id}` and `audit:global`

Every proxied call (allowed or denied) gets one `XADD` record:

```
trace_id, span_id, agent_id, session_id,
kind (llm|mcp|a2a), target, tool,
input, output, ts,
denied (bool), denial_reason
```

`trace_id` + `span_id` link directly to the Jaeger trace for the same call.

### Metrics (RedisTimeSeries)

One `TS.ADD` per call on the response hook. Key structure:

```
ts:{agent_id}:llm_tokens
ts:{agent_id}:tool_calls
ts:{agent_id}:a2a_calls
ts:{agent_id}:denials
```

Used for time-window rate limit pre-checks and dashboard queries.

### Traces (OTel → Jaeger)

Dedicated `Tracer` addon owns span lifecycle:
- Opens span in `request` hook; stores in `flow.metadata["otel_span"]`
- All other addons add attributes to `flow.metadata["otel_span"]`
- Closes span in `response` hook

Jaeger all-in-one runs inside the platform container under supervisord
(Badger persistence volume, ports 16686 UI + 4317 OTLP).

---

## 7. Identity

- Every session gets a bearer token from Orchestrator at session-create.
- Agent containers receive the token in `AMAZE_SESSION_TOKEN` env.
- SDK attaches `Authorization: Bearer <token>` on every outbound request.
- Proxy resolves the token against Redis (`session_token:{token}` →
  `agent_id`), STRIPS any client-supplied `x-amaze-caller`, then INJECTS the
  trusted `x-amaze-caller: <agent_id>` header before forwarding.
- Receiving SDK reads only the injected header — never `params.from` or
  any other client-controlled field. This is the spoof-proof identity
  invariant.

---

## 8. Connectivity Model

The platform exposes its proxy + orchestrator on host-published ports.
Agents reach them by IP/DNS — no internal-bridge isolation.

**Reachability:**
- Proxy on `:8080`, orchestrator on `:8001` (configurable in
  `docker/docker-compose.yml`).
- Agents set `HTTP_PROXY` / `HTTPS_PROXY` and `AMAZE_ORCHESTRATOR_URL`
  to the platform's address.
  - Same Docker host → `host.docker.internal:<port>` (Linux requires
    `extra_hosts: host-gateway`, already set in `examples/compose.yml`).
  - Different host → public DNS or LAN IP (e.g. `amaze.example.com`).
- Single env var `AMAZE_PROXY_HOST` controls the URL in the demo compose;
  remote agents override it per-host.

**What is NOT enforced by the network anymore:**
- Egress restriction. Previously, `internal: true` on `amaze-agent-net`
  guaranteed agents had no route to the public internet. With the bridge
  removed, the agent's host has its own route. Whether the agent uses the
  proxy is now policy + voluntary cooperation by the agent's HTTP_PROXY
  configuration. The proxy still enforces what an agent CAN do (policy);
  it cannot enforce what an agent's host CAN reach via direct egress.
- DNS-name routing for peer A2A across hosts. The proxy resolves peer
  agent hostnames against its own network namespace. Co-resident agents
  (same Docker host as the platform) work today via Docker DNS. Remote
  agents need an agent endpoint registry — planned, not yet implemented.

**Identity is per-agent, not per-network.** See §7. Bearer auth + Redis-
backed policy enforcement are the security boundary; the network is just
the transport.

**Hardening for production-ish remote-agent deployment** (planned, not in
this revision): TLS on the proxy's inbound port; `GET /ca.pem` on the
orchestrator for SDK CA bootstrap; agent endpoint registry for cross-host
A2A; mTLS or HMAC on the `x-amaze-caller` injection so remote receivers
can verify the call really came through the proxy.

**Per-agent container hardening** (still recommended where the operator
controls the agent container): `cap_drop=[ALL]`,
`security_opt=[no-new-privileges]`, `read_only=true`,
`tmpfs=/tmp (noexec,nosuid)`, `mem_limit`, `pids_limit=512`, `cpu_quota`.
The platform doesn't enforce these — they're operator hygiene.

---

## 9. Failure Handling

| Condition | Action |
|---|---|
| Policy addon raises | DENY (fail closed) |
| Unknown bearer token | DENY `invalid-bearer` |
| Unknown target agent | DENY `not-allowed` |
| Unknown MCP server | DENY `mcp-not-allowed` |
| Unknown MCP tool | DENY `tool-not-allowed` |
| Graph violation (`on_violation: block`) | DENY `graph_violation` + alert |
| Graph violation (`on_violation: allow`) | PASS + alert |
| Budget exceeded (`on_budget_exceeded: block`) | DENY `budget_exceeded` + alert |
| Budget exceeded (`on_budget_exceeded: allow`) | PASS + alert |
| Rate limit exceeded | DENY `rate-limit-exceeded` |
| Redis unavailable | DENY (503) |
| Malformed request | DENY |

---

## 10. Environment

- **Python venv:** `/home/ubuntu/venv/`. Always use `/home/ubuntu/venv/bin/python`
  and `/home/ubuntu/venv/bin/pip`. Never use system `python3`/`pip3`.

---

## 11. Bash command permissions

Commands that operate inside `/home/ubuntu/data/cloude/newAmazeControlPlane`
may be run without asking. Ask first only when the command:

- operates outside this directory,
- uses `sudo`,
- changes file permissions (`chmod`), ownership (`chown`), or group (`chgrp`),
- deletes or modifies files outside this directory.

---

## 12. Scrum Process

**Every iteration must deliver a working demo.** No "almost done" — each
sprint ends with something runnable.

### Sprint Rules

1. **Demo first** — each iteration ends with a browser/app demo the user can
   interact with. If it can't be shown, it isn't done.
2. **System tests per iteration** — every sprint ships at least one
   system-level test. Tests are discussed and agreed with the user *before*
   implementation starts.
3. **Test list is collaborative** — before writing any test, present the
   proposed test scenarios to the user and get sign-off. Never write tests
   unilaterally.
4. **No skipping iterations** — if a feature isn't ready, ship a smaller
   slice that is working rather than delivering nothing.
5. **Code review at sprint end** — run `/code-reviewer` on all new/modified
   files at the end of every sprint. Present the full results to the user
   and wait for instructions on what to fix.

### Sprint Task Format

Every sprint is broken into tasks with assigned roles:

| Role | Responsibility |
|---|---|
| Arch | Design decisions, schemas, data models |
| Dev | Implementation |
| QA | System tests |
| GUI | UI implementation (Sprint 3+) |

Tasks include explicit dependencies and are grouped into parallel phases.

### Parallel Execution

Independent tasks (no shared files, no data dependencies) **must** be run in
parallel using background agents. The rules:

1. **Identify phases** — group tasks into phases where every task within a
   phase has no file overlap with any sibling in the same phase.
2. **Launch simultaneously** — send all agents in a single message so they
   execute concurrently. Never launch them one at a time.
3. **No shared files per phase** — if two tasks in a proposed phase would
   touch the same file, split them into sequential phases.
4. **Timing log** — record each agent's wall-clock start time, end time, and
   token count as it completes. Write results to `sprint_timing.md` (or
   append to Progress.md) at the end of the sprint so cost and parallelism
   gains are visible.

Timing table format (append to Progress.md at sprint end):

```
| Task | Start | End | Duration | Tokens |
|------|-------|-----|----------|--------|
| T3-1 | 14:02 | 14:07 | 5 min | ~4 200 |
```

### System Tests (not unit tests)

- Tests must exercise the full stack end-to-end (agent → proxy → addon →
  decision).
- No mocking of the enforcement path.
- Never use mocks in system tests.
- Each test must be runnable as a standalone command.
- Every test verifies: (1) correct HTTP response, (2) correct audit log
  sequence in Redis Stream, (3) matching OTel trace in Jaeger.
- Use `agent_sdk.py`, `agent_sdk1.py`, `agent_sdk2.py` as drivers — do not
  modify them.

### Tracking Files

- **`SPRINTS.md`** — full sprint plan with all agreed system tests per sprint;
  updated at sprint start. Task rows include a status column (☐ / ◐ / ✓).
- **`Progress.md`** — running log of what has been completed; updated as work
  is finished. Includes timing table for parallel tasks at sprint end.

---

## 13. Key Rules for Claude

- **Never change architecture on your own judgement.** Raise the question,
  explain the trade-off, and wait for explicit approval before making any
  architectural change.
- Do NOT introduce new abstractions beyond what the task requires.
- Do NOT merge A2A / MCP / LLM / Graph enforcement into one code path — keep
  them distinct even when they share helpers.
- Do NOT add AI/LLM-based reasoning to the policy logic. Deterministic rules
  only.
- Do NOT add LLM nodes to the execution graph.
- Do NOT reintroduce Postgres. Redis + YAML is the permanent stack.
- Prefer simple, enforceable rules over clever logic.
- Never commit secrets. `.env` and any `*.secret.*` files stay out of git.

---

## 14. Mental Model

| Layer | Role |
|---|---|
| A2A | Network-level authorization |
| MCP | Capability-level authorization |
| LLM | Provider/model/token authorization |
| Graph | Execution-order authorization |
| Proxy (mitmproxy + addons) | Enforcement point |
| Addon chain | Decision engine (in-process, ordered, fail-closed) |
| Audit log (Redis Streams) | Immutable call record |
| Metrics (RedisTimeSeries) | Time-series usage data |
| Traces (OTel → Jaeger) | Distributed span visibility |
