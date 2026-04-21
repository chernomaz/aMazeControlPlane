# Progress

Running log. Newest at top.

---

## 2026-04-21 — Sprint S1 first boot

Platform container builds and boots clean after two small fixups:

- `docker/docker-compose.yml` — corrected build dockerfile path
  (`docker/Dockerfile` → `Dockerfile`, since the file lives at repo root).
- `docker/supervisord.conf` — dropped `[unix_http_server]`, `[rpcinterface:supervisor]`,
  `[supervisorctl]` sections; the apt `supervisor` package installs the
  binary but doesn't populate the system Python path that those sections
  import from, and we don't need `supervisorctl` for nodaemon mode.

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

This effectively covers **ST-S1.1** (boot), **ST-S1.2** (agent register),
**ST-S1.3** (MCP register), **ST-S1.8** (invalid bearer), and most of
**ST-S1.9** (fail-closed on unknown host/model) — pending automated test
scripts that will pin these.

Note: ST-S1.6 spec asks for reason `mcp-not-allowed` on unknown server,
but current implementation returns `host-not-allowed` because the
classifier gates on Redis-registration, not hostname pattern. Either tweak
the spec or add a classifier hint; tracked for the post-demo review.

---

## 2026-04-21 — Sprint S1 scaffold + code landed

Four-layer build-out across five commits:

- `a919d91` scaffold (SDK, agents, MCP server, Docker isolation)
- `9ef85a6` plan: self-registration, 2-file config, demo-mcp, 12 tests
- `cf21fe5` layer 1: Dockerfile, supervisord, config YAMLs
- `4cf47f0` layer 2: orchestrator (register + resolve MCP)
- `6dff8ff` layer 3: proxy addons (identity, enforcer, counters)
- `bbce1d6` layer 4: demo compose, agent & MCP Dockerfiles, SDK /register
