# Remote Routing Architecture

**Sprint S3 — 2026-04-29**

---

## Problem

S1/S2 assumed all containers run on the same Docker host and share a compose
network. The proxy resolved A2A and MCP targets via Docker DNS:

```
flow.request.host = "agent-sdk1"
→ Docker DNS → 172.18.0.5  (only works on shared amaze-agent-net)
```

This breaks when:
- Agents run on a different host from the platform
- Multiple agents run on the same host but on different Docker projects
  (no shared network, so Docker DNS does not resolve across projects)

---

## Solution

Replace Docker-DNS resolution with **Redis-backed endpoint registration**.

Every agent and MCP server registers its publicly reachable `host:port` with
the orchestrator at startup. The proxy's new `Router` addon looks up that
address in Redis and rewrites `flow.request.host + port` before mitmproxy
opens the upstream connection.

The transparent proxy model (`HTTP_PROXY`) is **unchanged**. Agents still
address targets by their logical name (`agent-sdk1`, `demo-mcp`).
`PolicyEnforcer` still classifies by `Host` header. The Router addon fires
last — after all enforcement — and silently redirects the connection to the
registered address.

---

## Registration Flow

```
1. Agent starts.
   Reads env: AMAZE_A2A_HOST, AMAZE_A2A_PORT

2. Agent calls POST http://{AMAZE_CONTROL_URL}/register
   Body: { "agent_id": "agent-sdk1",
           "a2a_host": "host.docker.internal",
           "a2a_port": 9003 }

3. Orchestrator:
   - mints bearer_token + session_id
   - writes Redis keys:
       session_token:{token}          → "agent-sdk1"        (24h TTL)
       session:{sid}:agent            → "agent-sdk1"        (24h TTL)
       agent_session:{agent-sdk1}     → session_id          (24h TTL)
       agent:agent-sdk1:endpoint      → "http://host.docker.internal:9003"  (24h TTL)

4. Orchestrator returns: { session_id, bearer_token, agent_id }

5. Agent sets bearer_token in SDK config.
   All outbound calls now carry X-Amaze-Bearer header (via httpx hook).
```

MCP servers already self-register via `POST /register?kind=mcp`. No change
needed there — `mcp:{name}` already stores the URL.

---

## Routing Flow (A2A)

```
agent-sdk wants to call agent-sdk1.

agent-sdk code (unchanged):
  httpx.post("http://agent-sdk1/a2a/tasks", ...)
  → routed via HTTP_PROXY to proxy:8080

Wire (unchanged from S1/S2):
  POST http://agent-sdk1/a2a/tasks HTTP/1.1   ← absolute URI, proxy format
  Host: agent-sdk1
  X-Amaze-Bearer: <token>

Proxy addon chain:
  SessionIdentity  → resolves bearer → "agent-sdk"          (unchanged)
  Tracer           → opens OTel span                         (unchanged)
  PolicyEnforcer   → host="agent-sdk1" ∈ allowed_agents → permit
                     sets amaze_kind="a2a", amaze_target="agent-sdk1"
                     injects x-amaze-caller: agent-sdk       (unchanged)
  GraphEnforcer    → step check                              (unchanged)
  StreamBlocker    → (no-op for A2A)                        (unchanged)
  Counters         → pre-check agent call budget             (unchanged)
  AuditLog         → records request                         (unchanged)
  Router [NEW]     → GET agent:agent-sdk1:endpoint
                     → "http://host.docker.internal:9003"
                     flow.request.host = "host.docker.internal"
                     flow.request.port = 9003

mitmproxy connects to host.docker.internal:9003 and forwards.
agent-sdk1 receives the request at its A2A port.
```

## Routing Flow (MCP)

```
agent-sdk2 calls demo-mcp/web_search.

Wire:
  POST http://demo-mcp/mcp/ HTTP/1.1
  Host: demo-mcp

Proxy addon chain:
  ...PolicyEnforcer → looks up mcp:demo-mcp in Redis (existing)
                      tool="web_search" ∈ allowed_tools → permit
                      sets amaze_kind="mcp", amaze_mcp_server="demo-mcp"
  ...
  Router [NEW]     → GET mcp:demo-mcp
                     → { "url": "http://host.docker.internal:8000", "tools": [...] }
                     flow.request.host = "host.docker.internal"
                     flow.request.port = 8000

mitmproxy connects to host.docker.internal:8000 and forwards.
```

## Routing Flow (LLM)

**Unchanged.** Agent calls `api.openai.com` via `HTTPS_PROXY`. mitmproxy
MITM's the TLS tunnel. The Router addon detects `amaze_kind="llm"` and
returns without rewriting — the target is already the real LLM provider.

```
Router addon (LLM path):
  kind = flow.metadata.get("amaze_kind")
  if kind == "llm":
      return   ← no rewrite, mitmproxy forwards to api.openai.com as-is
```

---

## Redis Key Schema (additions)

| Key | Value | TTL | Set by |
|---|---|---|---|
| `agent:{agent_id}:endpoint` | `http://{host}:{port}` | 24h | Orchestrator on `/register` |

Existing keys unchanged:
- `session_token:{token}` → agent_id
- `session:{sid}:agent` → agent_id
- `agent_session:{aid}` → session_id
- `mcp:{name}` → `{url, tools}`

---

## New Orchestrator Endpoint

```
GET /resolve/agent/{agent_id}

Response 200:
  { "agent_id": "agent-sdk1",
    "endpoint": "http://host.docker.internal:9003" }

Response 404:
  { "detail": "agent-not-registered" }
```

Used by the Router addon and for debugging. Mirrors the existing
`GET /resolve/mcp/{name}` endpoint.

---

## New Environment Variables

| Variable | Set on | Default | Purpose |
|---|---|---|---|
| `AMAZE_A2A_HOST` | Agent container | `host.docker.internal` | Hostname at which this agent's A2A port is reachable **from the proxy** |
| `AMAZE_A2A_PORT` | Agent container | `9002` | Port this agent's A2A server listens on AND the port the proxy routes to |

`AMAZE_A2A_HOST` answers: *"at what address can the proxy reach me?"*
Not necessarily the same as `AMAZE_PROXY_HOST` (which is the platform address
from the agent's perspective). For same-host deployments both resolve to
`host.docker.internal`. For different-host deployments they are different IPs.

---

## Deployment Scenarios

### Same host, multiple agents, no shared Docker network

```
Platform:   amaze-platform container, ports 8001 + 8080 on host
Redis:      amaze-redis container, 127.0.0.1:6379 on host

agent-sdk:  separate compose project, no amaze-agent-net
            AMAZE_PROXY_HOST=host.docker.internal
            AMAZE_A2A_HOST=host.docker.internal
            AMAZE_A2A_PORT=9002
            ports: 9002:9002

agent-sdk1: separate compose project
            AMAZE_A2A_HOST=host.docker.internal
            AMAZE_A2A_PORT=9003
            ports: 9003:9003   ← symmetric, no asymmetric mapping

agent-sdk2: separate compose project
            AMAZE_A2A_HOST=host.docker.internal
            AMAZE_A2A_PORT=9004
            ports: 9004:9004
```

### Different hosts

```
Platform host (203.0.113.10):
  amaze-platform, amaze-redis

Agent host A (198.51.100.20):
  AMAZE_PROXY_HOST=203.0.113.10
  AMAZE_A2A_HOST=198.51.100.20
  AMAZE_A2A_PORT=9002
  HTTP_PROXY=http://203.0.113.10:8080
  HTTPS_PROXY=http://203.0.113.10:8080

Agent host B (198.51.100.21):
  AMAZE_PROXY_HOST=203.0.113.10
  AMAZE_A2A_HOST=198.51.100.21
  AMAZE_A2A_PORT=9002        ← same port is fine, different IP
  HTTP_PROXY=http://203.0.113.10:8080
  HTTPS_PROXY=http://203.0.113.10:8080
```

---

## Router Addon — Placement in Chain

```
services/proxy/main.py addon order:

  FailClosed(SessionIdentity(),  "session")
  FailClosed(Tracer(),           "tracer")
  FailClosed(PolicyEnforcer(),   "enforcer")   ← sets amaze_kind, amaze_target
  FailClosed(GraphEnforcer(),    "graph")
  FailClosed(StreamBlocker(),    "stream_blocker")
  FailClosed(Counters(),         "counters")
  FailClosed(AuditLog(),         "audit_log")
  FailClosed(Router(),           "router")     ← NEW, always last
```

Router runs **after** all enforcement and audit. If any earlier addon denies
the request (`flow.response` is set), the FailClosed guard skips the Router —
the rewrite never happens on a denied flow. The audit record always reflects
the logical target name (`agent-sdk1`, `demo-mcp`), not the resolved IP.

---

## Failure Handling

| Condition | Router behaviour |
|---|---|
| Redis unavailable | Deny 503 `redis-unavailable` (fail-closed) |
| `agent:{id}:endpoint` key missing | Deny 503 `agent-not-registered` |
| `mcp:{name}` key missing | Already denied by PolicyEnforcer before Router runs |
| Registered endpoint unreachable | mitmproxy returns 502; audit records it |
| LLM call (`amaze_kind="llm"`) | Router no-ops; mitmproxy forwards to real provider |

---

## What Is Not Changed

- `HTTP_PROXY` / `HTTPS_PROXY` on agents
- mitmproxy configuration (`mitmdump` command, supervisord)
- All existing proxy addons (SessionIdentity, Tracer, PolicyEnforcer,
  GraphEnforcer, StreamBlocker, Counters, AuditLog)
- `PolicyEnforcer` host-based classification logic
- LLM path (MITM tunnel to `api.openai.com`)
- Bearer token / session identity model
- `config/policies.yaml` schema
- `config/mcp_servers.yaml` (MCP URL already stored in Redis)
- All S1/S2 system tests
