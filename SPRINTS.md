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
| ST-S1.10 | ☐ | Bitcoin (A2A + LLM) | User POSTs to `agent-sdk`: *"search for current bitcoin price"*. `agent-sdk` → LLM → routes to `agent-sdk1` (keyword "bitcoin") via A2A → `agent-sdk1` → LLM → result cascades back. | 200 end-to-end; Redis shows LLM-token counters incremented on both `agent-sdk` and `agent-sdk1`; A2A call count incremented on `agent-sdk`. |
| ST-S1.11 | ☐ | Weather (A2A + LLM + MCP allow) | User POSTs to `agent-sdk`: *"search for current weather in London"*. `agent-sdk` → LLM → routes to `agent-sdk2` via A2A → `agent-sdk2`'s LLM calls `demo-mcp/web_search` (policy allows) → result cascades back. | 200 end-to-end; `web_search` counter on `agent-sdk2` incremented; upstream MCP call observed. |
| ST-S1.12 | ☐ | NY news (MCP tool deny) | User POSTs to `agent-sdk`: *"search for current NEW YORK news"*. Routes to `agent-sdk2`; agent-sdk2's LLM attempts `demo-mcp/dummy_email` — not in `agent-sdk2`'s allowlist. | Proxy returns 403 `tool-not-allowed`; MCP upstream **not** hit; `agent-sdk2` surfaces a "tool not permitted" error to the caller; final user response contains the error, not a successful email read. |

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
