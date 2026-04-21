# Progress

Running log. Newest at top.

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
