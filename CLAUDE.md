# aMaze Control Plane

Runtime enforcement layer for agent systems. Fresh wiring of the SDK + proxy +
orchestrator + registry, collapsed into a single container with YAML-driven
config.

---

## 1. Project Overview

Enforces:

- **A2A (Agent-to-Agent)** — who can talk to whom.
- **MCP (Model Context Protocol)** — which tools/resources an agent may call.
- **LLM** — which providers/models/token budgets an agent is allowed.

All outbound traffic from agent containers is forced through a single proxy
(mitmproxy) on a Docker network with no direct internet route. The proxy runs
the policy logic in-process via an addon — there is no separate policy
service. Policies are static YAML, loaded at boot. Live state (session
counters, bearer tokens) lives in Redis.

---

## 2. Core Principles

- All agent traffic MUST go through the proxy.
- No direct network access from agent runtimes.
- Proxy-only egress is mandatory — enforced by `internal: true` Docker network.
- Policy enforcement is real-time and **fails closed** — if the addon raises,
  the request is denied.
- Strict separation of concerns: A2A ≠ MCP ≠ LLM.
- Deterministic rules over "smart" heuristics.

---

## 3. Scope

- **A2A enforcement** — allow/deny between agents via `allowed_remote_agents`.
- **MCP enforcement** — allow/deny per server and per tool.
- **LLM enforcement** — allow/deny per provider + model; token budgets.
- **Basic limits** — rate limits, request size, per-tool call counts.

### Out of scope (do not implement)

- UI/dashboard (later sprint; not step 1).
- ExecutionGraph / DAG ordering (deferred to step 2).
- Postgres persistence (Redis + YAML only for step 1).
- Graph execution policies.
- Intention drift detection.
- Prompt analysis / AI guardrails.
- Trust scoring.
- Testing framework.
- Multi-agent orchestration logic.
- LangChain-specific instrumentation.

---

## 4. Architecture

```
Agent container (isolated net, HTTP_PROXY forced)
    ↓
Proxy (mitmproxy + addons: session_id, policy, router, token_counter)
    ↓
Upstream:
   - Peer agent containers (A2A)
   - MCP servers (tool calls)
   - LLM providers (openai, anthropic, ...)
```

Running processes (supervisord, one container):

| Process | Port | Role |
|---|---|---|
| redis | 6379 | session state, counters, bearer tokens |
| orchestrator | 8001 | session lifecycle + Docker spawn + MCP/agent resolver (absorbs what aMaze called "registry") |
| proxy | 8080 | mitmproxy + policy-enforcement addon |

The orchestrator owns both container lifecycle and name resolution:
`GET /resolve/mcp/{name}` → MCP server URL + tool list (read from
`config/mcp_servers.yaml` at boot); `GET /resolve/agent/{session_id}/{agent_id}` →
live agent container URL (written in-memory by the spawn path).

---

## 5. Config (step 1)

Three YAMLs under `config/`, loaded at boot:

- `policies.yaml` — per-agent `allowed_remote_agents`, `allowed_mcp_servers`,
  `allowed_tools`, `allowed_llms`, `limits`.
- `agents.yaml` — image, policy ref, env, command per agent.
- `mcp_servers.yaml` — name → URL + tool list.

Reload requires proxy restart (step 1 simplification; dynamic reload is a
later sprint).

---

## 6. Identity

- Every session gets a bearer token from Orchestrator at session-create.
- Agent containers receive the token in `AMAZE_SESSION_TOKEN` env.
- SDK attaches `Authorization: Bearer <token>` on every outbound request.
- Proxy resolves the token against Redis (`session_token:{token}` →
  `agent_id`), STRIPS any client-supplied `x-amaze-caller`, then INJECTS the
  trusted `x-amaze-caller: <agent_id>` header before forwarding.
- Receiving SDK reads only the injected header — never `params.from` or
  any other client-controlled field. This is the spoof-proof identity
  invariant (cf. the Sprint 9 fix in the predecessor repo).

---

## 7. Container Isolation

Agent containers are spawned with:

- `network=amaze-agent-net` (internal=true — no default route).
- `HTTP_PROXY` / `HTTPS_PROXY` → proxy URL.
- `cap_drop=[ALL]`, `security_opt=[no-new-privileges]`.
- `read_only=True`, `tmpfs=/tmp (noexec,nosuid)`.
- `mem_limit`, `pids_limit=512`, `cpu_quota`.
- Proxy MITM CA mounted read-only at
  `/usr/local/share/ca-certificates/amaze-proxy.crt`.

See `docker/container_spawn.py` and `docker/compose.networks.yml`.

---

## 8. Failure Handling

| Condition | Action |
|---|---|
| Policy addon raises | DENY (fail closed) |
| Unknown bearer token | DENY `invalid-bearer` |
| Unknown target agent | DENY `not-allowed` |
| Unknown MCP server | DENY `mcp-not-allowed` |
| Unknown MCP tool | DENY `tool-not-allowed` |
| Redis unavailable | DENY (503) |
| Malformed request | DENY |

---

## 9. Environment

- **Python venv:** `/home/ubuntu/venv/`. Always use `/home/ubuntu/venv/bin/python`
  and `/home/ubuntu/venv/bin/pip`. Never use system `python3`/`pip3`.

---

## 10. Bash command permissions

Commands that operate inside `/home/ubuntu/data/cloude/newAmazeControlPlane`
may be run without asking. Ask first only when the command:

- operates outside this directory,
- uses `sudo`,
- changes file permissions (`chmod`), ownership (`chown`), or group (`chgrp`),
- deletes or modifies files outside this directory.

---

## 11. Scrum Process

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

### System Tests (not unit tests)

- Tests must exercise the full stack end-to-end (agent → proxy → addon →
  decision).
- No mocking of the enforcement path.
- Never use mocks in system tests.
- Each test must be runnable as a standalone command.

### Tracking Files

- **`SPRINTS.md`** — full sprint plan with all agreed system tests per sprint;
  updated at sprint start.
- **`Progress.md`** — running log of what has been completed; updated as work
  is finished.

---

## 12. Key Rules for Claude

- Do NOT introduce new abstractions beyond what the task requires.
- Do NOT merge A2A / MCP / LLM enforcement into one code path — keep them
  distinct even when they share helpers.
- Do NOT add AI/LLM-based reasoning to the policy logic. Deterministic rules
  only.
- Do NOT reintroduce Postgres in step 1. Use Redis + YAML until the durability
  sprint.
- Prefer simple, enforceable rules over clever logic.
- Never commit secrets. `.env` and any `*.secret.*` files stay out of git.

---

## 13. Mental Model

| Layer | Role |
|---|---|
| A2A | Network-level authorization |
| MCP | Capability-level authorization |
| LLM | Provider/model/token authorization |
| Proxy (mitmproxy + addon) | Enforcement point |
| Addon module | Decision engine (in-process, not a separate service) |
