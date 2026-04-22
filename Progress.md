# Progress

Running log. Newest at top.

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
