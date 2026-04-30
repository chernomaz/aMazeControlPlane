# Sprint S3 Demo — Remote Agent + MCP Runtime MVP

**Date:** 2026-04-29

---

## What Was Built

Sprint S3 removes the requirement for agents and MCP servers to share a Docker
network with the platform. Previously, the proxy resolved A2A and MCP targets
via Docker DNS — which only worked when every container was in the same
`amaze-agent-net` bridge. S3 replaces that with Redis-backed endpoint
registration.

### Core change: Redis-backed endpoint registration + Router addon

```
Before (S1/S2):
  agent calls http://agent-sdk1/...
  → proxy forwards to agent-sdk1 via Docker DNS (same network required)

After (S3):
  agent registers: POST /register { a2a_host: "host.docker.internal", a2a_port: 9003 }
  orchestrator writes: agent:agent-sdk1:endpoint = "http://host.docker.internal:9003"

  agent calls http://agent-sdk1/...
  → PolicyEnforcer classifies: amaze_kind=a2a, amaze_target=agent-sdk1
  → AuditLog writes record with logical name "agent-sdk1"
  → Router reads agent:agent-sdk1:endpoint from Redis
  → Router rewrites: flow.request.host = host.docker.internal, port = 9003
  → mitmproxy connects to the real address
```

### Files delivered

| File | What changed |
|---|---|
| `docs/remote-routing-architecture.md` | Architecture doc (S3-T1) |
| `services/proxy/router.py` | New Router addon (S3-T4) |
| `services/proxy/main.py` | Router wired last in chain (S3-T4) |
| `services/orchestrator/main.py` | `a2a_host+a2a_port` in `/register`; new `GET /resolve/agent/{id}` (S3-T2) |
| `sdk/amaze/_core.py` | Reads `AMAZE_A2A_HOST`; sends endpoint in registration (S3-T3) |
| `examples/compose.yml` | `AMAZE_A2A_HOST`, symmetric port mappings per agent (S3-T5) |
| `docker/compose-agent-host.yml` | Separate compose project for agents (no shared network) (S3-T5) |
| `docker/compose-mcp-host.yml` | Separate compose project for MCP server (S3-T5) |
| `scripts/demo_remote_runtime.sh` | Automated demo script (S3-T5) |
| `tests/test_s3.py` | 9 system tests ST-S3.1 through ST-S3.7 (S3-T6) |
| `tests/conftest.py` | Backward-compat fixture for S2 tests (S3-T6) |
| `config/policies.yaml` | 3 new S3 test agent policies (S3-T6) |

---

## Demo: Same-host multi-project routing

### Prerequisites

```bash
# Clone / enter the repo
cd /path/to/newAmazeControlPlane/aMaze

# Required env vars
export OPENAI_API_KEY=sk-...
export TAVILY_API_KEY=tvly-...

# Optional: override host if platform runs on a different IP
# export AMAZE_PROXY_HOST=192.168.1.100
```

### Step 1 — Start the platform

```bash
docker compose -f docker/docker-compose.yml up -d --build
# Waits for Redis healthy, starts orchestrator + proxy + Jaeger
# Ports: 8001 (orchestrator), 8080 (proxy), 16686 (Jaeger)
```

### Step 2 — Start MCP server (separate compose project)

```bash
docker compose \
  -f docker/docker-compose.yml \
  -f docker/compose-mcp-host.yml \
  up -d --build
```

The MCP server registers itself:

```bash
# Verify in Redis
docker exec amaze-redis redis-cli GET mcp:demo-mcp
# → {"url": "http://host.docker.internal:8000", "tools": ["web_search", ...]}
```

### Step 3 — Start agents (separate compose project, no shared network)

```bash
docker compose \
  -f docker/docker-compose.yml \
  -f docker/compose-agent-host.yml \
  up -d --build
```

Each agent registers with its `AMAZE_A2A_HOST` + `AMAZE_A2A_PORT`. Verify:

```bash
docker exec amaze-redis redis-cli GET agent:agent-sdk:endpoint
# → http://host.docker.internal:9002

docker exec amaze-redis redis-cli GET agent:agent-sdk1:endpoint
# → http://host.docker.internal:9003

docker exec amaze-redis redis-cli GET agent:agent-sdk2:endpoint
# → http://host.docker.internal:9004
```

### Step 4 — Run the demo prompt

```bash
# Bitcoin: agent-sdk → LLM → A2A to agent-sdk1 → LLM + web_search → reply
curl -s -XPOST http://localhost:8090/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"search for current bitcoin price"}' | jq .

# Weather: agent-sdk → A2A to agent-sdk2 → LLM + MCP web_search → reply
curl -s -XPOST http://localhost:8090/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"search for current weather in London"}' | jq .
```

### Step 5 — Inspect audit logs

```bash
# See all calls for agent-sdk (LLM + A2A)
docker exec amaze-redis redis-cli XRANGE audit:agent-sdk - + | head -60

# See A2A routing in action — the Router rewrites host:port,
# but the audit record still shows the logical name "agent-sdk1"
docker exec amaze-redis redis-cli XRANGE audit:agent-sdk - + | grep "agent-sdk1"
```

### Step 6 — Inspect traces in Jaeger

Open http://localhost:16686 → select service `amaze.proxy` → search.

Each conversation has a single `trace_id` shared across all hops (agent-sdk →
agent-sdk1 → LLM calls). The Router rewrite is transparent — Jaeger shows
the logical target names, not the resolved IPs.

---

## Automated Demo Script

```bash
./scripts/demo_remote_runtime.sh
```

The script:
1. Starts all three compose stacks
2. Polls health endpoints until ready
3. Verifies Redis keys for all registered agents and MCP
4. POSTs the bitcoin prompt and shows the reply
5. Dumps the agent-sdk audit log

---

## System Test Run

```bash
# Start the test compose stack
docker compose -f tests/compose.test.yml up -d --build

# Run S3 tests (9 tests covering ST-S3.1 through ST-S3.7)
/home/ubuntu/venv/bin/pytest tests/test_s3.py -v

# Also verify S2 tests still pass with the new Router
/home/ubuntu/venv/bin/pytest tests/test_s2.py -v
```

### Test summary

| ID | Name | Verifies |
|---|---|---|
| ST-S3.1 | Remote registration | `agent:{id}:endpoint` in Redis; `GET /resolve/agent/{id}` returns 200 |
| ST-S3.1b | Unregistered resolve 404 | `/resolve/agent/unknown` returns 404 `agent-not-registered` |
| ST-S3.2 | MCP allowed | Router routes to mock-mcp; 200; audit + tool counter |
| ST-S3.3 | MCP denied tool | `dummy_email` not in policy; 403 `tool-not-allowed`; audit written |
| ST-S3.4 | A2A allowed (Router) | Router reads `agent:test-s3-callee:endpoint`, routes to mock-agent:8000; 200 |
| ST-S3.4b | A2A fail-closed | Delete endpoint key → 503 `agent-not-registered` (Router fail-closed) |
| ST-S3.5 | A2A denied | Agent calls non-allowed peer; 403 `host-not-allowed` before Router fires |
| ST-S3.6 | LLM allowed | Router no-ops for `amaze_kind=llm`; 200; token counter incremented |
| ST-S3.7 | LLM denied | No-LLM policy; 403 `llm-not-allowed`; Router not reached |

---

## What Is Unchanged

- `HTTP_PROXY` / `HTTPS_PROXY` agent configuration
- mitmproxy configuration (`mitmdump` command, supervisord)
- All existing proxy addons (SessionIdentity, Tracer, PolicyEnforcer,
  GraphEnforcer, StreamBlocker, Counters, AuditLog)
- `PolicyEnforcer` host-based classification logic
- LLM path (MITM tunnel to `api.openai.com`)
- Bearer token / session identity model
- `config/policies.yaml` schema
- All S1/S2 system tests (backward compatible via `tests/conftest.py` fixture)

---

## Deployment Scenarios Enabled

| Scenario | Before S3 | After S3 |
|---|---|---|
| All containers on one host, shared network | ✓ | ✓ |
| Same host, separate Docker projects (no shared network) | ✗ | ✓ |
| Agents on a different host from the platform | ✗ | ✓ |
| Multiple agents on same host, different ports | ✗ | ✓ |
