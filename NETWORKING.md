# Networking — aMaze Substrate (Sprint 8+)

This document describes the network topology, traffic paths, and
enforcement points introduced through Sprint 8 and extended in
Sprint 9. It is scoped to what is actually built and tested in
`./run_sprint9.sh` — no aspirational features.

Sprint 9 layered three additional agent containers (`sdk-agent-a`,
`sdk-agent-b`, `sdk-agent-llm`) on top of the Sprint 8 substrate; the
topology section below reflects the 5-agent stack. Routing rules
stay the same shape — each new agent gets its own virtual host +
STRICT_DNS cluster in Envoy, identical to `agent-a` / `agent-b`.

> If you just want "what runs where", skip to [Topology](#topology).
> If you want "where is policy enforced", jump to [Enforcement points](#enforcement-points).

---

## Why a substrate

The core invariant of the control plane is:

> All agent traffic goes through Envoy. Envoy calls an external policy
> processor via ext_proc. Agents cannot reach any upstream directly.

Sprint 8 operationalises that invariant inside Docker Compose so we can
demo the full flow end-to-end without any agent code changes.

The hard constraints that shape the design:

1. **Zero changes to the agent author's source.** A `openai.OpenAI()`
   call must end up on the real OpenAI API without the author knowing
   Envoy / LiteLLM / ext_proc exist.
2. **Envoy must see plaintext for every hop it enforces.** A CONNECT
   tunnel (the default when httpx sees an `https://` URL with
   `HTTPS_PROXY` set) hides the body and breaks `ext_proc`.
3. **Fail closed.** Every proxy / policy lookup denies on failure. No
   allowlist lookup returning `unknown` is ever interpreted as `allow`.

Everything else in this document is a consequence of those three rules.

---

## Topology

All NEMO services run on a single Docker bridge network called
`amaze-net` and are brought up by
`docker/docker-compose.yml`. From a network perspective the
stack is one L2 segment with the following nodes:

```
                                             ┌────────────────────────┐
                                             │  api.openai.com:443    │   (real
                                             │  api.anthropic.com:443 │    internet,
                                             └────────────▲───────────┘    TLS)
                                                          │
                                                          │ HTTPS originated by
                                                          │ LiteLLM sidecar
                                                          │
                  ┌────────── amaze-net (compose bridge) ───────────────────────────────┐
                  │                                                                     │
   host :7000 ────┤ orchestrator :7000      policy-processor :50051 ext_proc             │
                  │   (registration,          (Go; DecideA2A/MCP/LLM; stats HTTP :8081; │
                  │    policy/token push,     config HTTP :8082; reverse token lookup)  │
                  │    chat relay)                     ▲                                │
                  │   static GUI              gRPC ext_proc calls                       │
                  │                                    │                                │
   host :10000 ───┤ envoy :10000 ───── http_filters.ext_proc ──┘                        │
                  │   (listener for ALL agent traffic)                                  │
                  │      virtual hosts match on :authority/host →                       │
                  │      clusters of upstreams                                          │
                  │                                                                     │
                  │     ┌──── http://litellm:4000 ─────────────────┐                    │
                  │     │    plaintext HTTP in; TLS out to         │                    │
                  │     │    api.openai.com / api.anthropic.com    │                    │
                  │     └──────────────────────────────────────────┘                    │
                  │                                                                     │
                  │     ┌──── http://a2a-proxy:8082 ──────────────┐                     │
                  │     │    plaintext HTTP in; TLS out to the    │                     │
                  │     │    partner FQDN in the Host header      │                     │
                  │     └──────────────────────────────────────────┘                    │
                  │                                                                     │
                  │     ┌── http://mcp-5-tools:8000 ──────────────┐                     │
                  │     │    plaintext streamable-http MCP server │                     │
                  │     └──────────────────────────────────────────┘                    │
                  │                                                                     │
   host :18080 ──┤ agent-a :8080 (chat) :9002 (A2A)   agent-b :8080 :9002 ── host :28080│
                  │   HTTP_PROXY=envoy:10000, HTTPS_PROXY=envoy:10000                   │
                  │   NO_PROXY=orchestrator,policy-processor,envoy,localhost,127.0.0.1  │
                  │                                                                     │
                  │     ┌── partner-agent :443 (DNS alias partner-agent.example.com) ──┐│
                  │     │   self-signed TLS cert, SAN=partner-agent.example.com         ││
                  │     └───────────────────────────────────────────────────────────────┘│
                  └─────────────────────────────────────────────────────────────────────┘
```

Ports mapped to the host are **only** the ones a human / test harness
needs: the orchestrator REST API (7000), Envoy (10000, plus its admin
on 9901), per-agent chat (18080/28080), and `127.0.0.1`-bound stats
(8081) and config (8082) on the policy processor. **Nothing else is
reachable from outside the compose network.** Agents cannot talk to
sidecars directly, cross-org targets are only reachable via the
sidecar, and so on.

---

## Services and their purpose

| Service          | Role                                                              | Internal addr              | Exposed to host                          |
| ---------------- | ----------------------------------------------------------------- | -------------------------- | ---------------------------------------- |
| orchestrator     | registration, policy/token push, chat relay, static GUI           | `orchestrator:7000`        | `0.0.0.0:7000`                           |
| policy-processor | Go ext_proc server + stats + config HTTP                          | `policy-processor:50051`   | `127.0.0.1:8081`, `127.0.0.1:8082`       |
| envoy            | traffic choke point; runs `envoy.nemo.yaml`                       | `envoy:10000`              | `0.0.0.0:10000`, `0.0.0.0:9901` (admin)  |
| litellm          | HTTPS-terminating LLM sidecar (OpenAI/Anthropic compat proxy)     | `litellm:4000`             | *not exposed*                            |
| a2a-proxy        | HTTPS-terminating A2A sidecar for cross-org traffic               | `a2a-proxy:8082`           | *not exposed*                            |
| mcp-5-tools      | FastMCP server with `web_search` tool (Tavily)                    | `mcp-5-tools:8000`         | *not exposed*                            |
| agent-a          | NEMO agent container (demo)                                       | `agent-a:8080`, `:9002`    | `0.0.0.0:18080` (chat only)              |
| agent-b          | NEMO agent container (demo)                                       | `agent-b:8080`, `:9002`    | `0.0.0.0:28080` (chat only)              |
| partner-agent    | fake "different organisation" A2A endpoint                        | `partner-agent.example.com:443` (DNS alias) | *not exposed* |

DNS alias trick: `partner-agent` in compose joins `amaze-net` with an
alias `partner-agent.example.com` so the a2a-proxy sidecar can dial it
by its real public FQDN without DNS tricks on the agent side.

---

## Enforcement points

Every outbound request from an agent container hits **two** enforcement
surfaces:

1. **Envoy** — matches on the `:authority`/`Host` header, picks an
   upstream cluster, invokes ext_proc for headers and body (and
   response body). Fails closed (`failure_mode_allow: false`).
2. **policy-processor (Go ext_proc)** — classifies the request by
   target + JSON-RPC method, runs `DecideA2A` / `DecideMCP` /
   `DecideLLM` against the in-memory policy + token registries, and
   returns `allow` or an `ImmediateResponse` with a structured
   `{"error":"denied","reason":"<code>"}` body.

Deny reasons seen in production are stable strings used by tests
verbatim:

```
not-allowed             mcp-server-not-allowed   tool-not-allowed
unknown-caller          request-too-large        rate-limit-exceeded
call-limit-exceeded     token-limit-exceeded     llm-not-allowed
invalid-bearer          missing-bearer           unknown-method
```

---

## Traffic paths

### 1. Internal A2A (agent-a → agent-b)

Sprint 8A + Slice 4 (bearer).

```
┌─ agent-a (container) ─────┐
│ POST /a2a-to/agent-b      │                  state.a2a_token set
│   inside nemo_agent.py    │                  at first register
│   httpx.AsyncClient(       │
│     proxy=http://envoy:10000,                headers:
│     ... json=payload,                          Authorization: Bearer <agent-a-token>
│   )                                            Host: agent-b  (default from URL)
└────────────────────┬──────┘                  NO x-agent-id (Slice 4 — bearer only)
                     │ plain HTTP
                     ▼
┌── envoy :10000 ───┐
│ HCM matches       │ ext_proc (gRPC :50051):
│ virtual_host      │   - extractIDs: Authorization:Bearer → tokens.Resolve → agent-a
│ "agent_b"         │   - bs=bearerResolved
│ → cluster         │   - method=tasks/send
│   agent_b_cluster │   - DecideA2A("agent-a", "agent-b", 146 bytes)
└────────┬──────────┘       → allowed_remote_agents contains "agent-b"
         │                  → PASS
         │ plain HTTP
         ▼
┌── agent-b :9002 ──┐
│  FastAPI /        │  returns A2A JSON-RPC envelope with
│  echoes message   │  {"artifacts":[{"parts":[{"text":"[agent-b] echo: ..."}]}]}
└───────────────────┘
```

Key Slice 4 detail: `nemo_agent.py`'s `/a2a-to/{target}` deliberately
does **not** send `x-agent-id`. The A2A spec has a native auth
primitive (`Authorization: Bearer`), so after Slice 4 that is the sole
identity carrier for A2A. The processor resolves the bearer via the
`tokens.Store`, which is kept in sync with the orchestrator through
`PUT /config/tokens/{id}`.

### 2. Cross-org A2A (agent-a → partner-agent.example.com)

Slice 5.

```
┌─ agent-a /cross-org-a2a ──┐
│ body = { target:           │
│  "https://partner-agent.   │  step 1: rewrite scheme https→http so
│   example.com/a2a", ... }  │          HTTP_PROXY routes through Envoy
│                            │          in plaintext (no CONNECT tunnel)
│ httpx.AsyncClient(          │
│   proxy=http://envoy:10000,│  headers:
│ ).post(                     │    Authorization: Bearer <agent-a-token>
│   "http://partner-agent.   │    Host: partner-agent.example.com  ← authoritative
│    example.com/a2a", ...)  │          for BOTH Envoy routing and sidecar dial
└──────────────┬──────────────┘
               │ plain HTTP
               ▼
┌── envoy :10000 ───┐
│ virtual_host      │  ext_proc:
│ partner_agent_    │    caller=agent-a (from bearer)
│ example_com       │    target=partner-agent.example.com (from Host)
│ → cluster         │    method=tasks/send
│   a2a_proxy_      │    DecideA2A ⇒ allowed_remote_agents contains
│   cluster         │      "partner-agent.example.com" ⇒ PASS
└────────┬──────────┘
         │ plain HTTP, Host preserved
         ▼
┌── a2a-proxy :8082 ───────────────────────┐
│   h := r.Host      → partner-agent.example.com
│   allowlist check  → ALLOWED_PARTNERS
│   upstream URL     → https://partner-agent.example.com/a2a
│   http.Client.Do   → TLS handshake (InsecureSkipVerify for self-signed)
└──────────┬───────────────────────────────┘
           │ HTTPS (real TLS)
           ▼
┌── partner-agent :443 ────────┐
│  FastAPI + uvicorn SSL       │  returns "cross-org echo: …"
│  cert SAN matches Host       │
└──────────────────────────────┘
```

The scheme rewrite is the entire point of Slice 5. Once agents move to
the patched A2A SDK in Sprint 9+, the SDK will do this rewrite
transparently; the Slice 5 helper endpoint does it explicitly to keep
the demo observable. The a2a-proxy forwards the agent's internal
bearer as-is today — production should strip it and inject a
partner-specific credential, which is future work.

### 3. LLM (agent-a → LiteLLM → api.openai.com)

Slice 3.

```
┌─ user agent code (unmodified) ─┐
│ import openai                   │     sdk_patches (imported at startup)
│ client = openai.OpenAI()        │ ──► monkey-patched __init__ sets:
│ client.chat.completions.create( │       base_url = http://envoy:10000/v1
│   model="gpt-4o-mini", ... )    │       default_headers = {
│                                 │         "host": "litellm",
│                                 │         "x-agent-id": AMAZE_AGENT_ID,
└───────────────┬─────────────────┘       }
                │
                │ NO_PROXY contains "envoy" so httpx hits Envoy
                │ as origin (NOT via HTTP_PROXY — would double-proxy)
                │
                ▼
┌── envoy :10000 ───┐
│ virtual_host      │  ext_proc:
│ "litellm"         │    caller=agent-a (from x-agent-id;
│ → cluster         │      bearer may be openai placeholder, unresolvable,
│   litellm_cluster │      so bs=bearerInvalid but extractIDs *falls back*
└────────┬──────────┘      to x-agent-id — this is intentional so the
         │                 openai SDK's Bearer-API-key pattern doesn't
         │ plain HTTP      crash every LLM call)
         │                 target=litellm, DecideLLM ⇒ allowed_llms
         ▼                 contains "litellm" ⇒ PASS
┌── litellm :4000 ─────┐
│  LiteLLM proxy       │  reads request, originates TLS to
│  (ghcr.io/berriai/   │  api.openai.com:443 using OPENAI_API_KEY
│   litellm:main-stable)│ from its own env (not agent's)
└──────────┬───────────┘
           │ HTTPS (real TLS)
           ▼
        OpenAI

… and the response flows back in reverse. Envoy's
`response_body_mode: BUFFERED` passes the full plaintext response
body to ext_proc, which calls `extractTokens` and either parses
`usage.total_tokens` directly or decompresses gzip first (OpenAI
returns gzip when the client sends Accept-Encoding: gzip, which
httpx does by default).
```

Two invariants that make this work:

- The patched SDK sends `base_url=http://envoy:10000/v1` with
  `Host: litellm`. Envoy's listener has a virtual host named `litellm`
  that routes to the LiteLLM container on port 4000. Envoy sees plain
  HTTP in and plain HTTP out, so ext_proc can inspect both.
- LiteLLM is the thing that speaks HTTPS to api.openai.com. Envoy does
  NOT originate TLS upstream for LLMs; that was the Sprint 6
  approach (`openai_cluster` → `api.openai.com:443`), and it still
  works outside of NEMO. In NEMO we chose LiteLLM because a single
  `litellm_cluster` covers N providers.

### 4. MCP (agent-a → mcp-5-tools)

Slice 2.

```
┌─ agent-a /mcp-call/{server}/{tool} ─┐
│ resolves host:port from              │
│   GET http://orchestrator:7000/mcp  │
│ fastmcp.Client(                     │
│   StreamableHttpTransport(          │  headers:
│     url="http://mcp-5-tools:8000    │    x-agent-id: agent-a
│           /mcp/",                   │    (no bearer — MCP has no auth spec yet)
│     headers={"x-agent-id": "agent-a"}))
│ httpx.AsyncClient trust_env=True    │  HTTP_PROXY=envoy routes it through.
└──────────────┬──────────────────────┘
               │ plain HTTP through Envoy
               ▼
┌── envoy :10000 ───┐
│ virtual_host      │   ext_proc sees the MCP streamable-http JSON-RPC
│ "mcp_5_tools"     │     tools/call → DecideMCP(agent-a, mcp-5-tools, tool, size)
│ → cluster         │       allowed_mcp_servers contains "mcp-5-tools"
│   mcp_5_tools_    │       allowed_tools["mcp-5-tools"] contains <tool>
│   cluster         │       + per_tool_calls + size + rate checks ⇒ PASS
└────────┬──────────┘   initialize/notifications/* etc. are classified as
         │ plain HTTP   protocol passthrough (mcpPassthrough set in processor.go)
         ▼
┌── mcp-5-tools :8000 ───┐
│  FastMCP server        │ runs the Python tool
│  streamable-http       │ returns JSON-RPC result
└────────────────────────┘
```

MCP traffic is the one that still relies on `x-agent-id`; the MCP spec
has no standard authentication header. The sidecar (Sprint 9+) or the
patched MCP client injects it deterministically; malicious agents
inside the container could spoof it, but the threat model assumes the
container itself is trusted — enforcement is about what the *running
agent code* can do, not about a compromised container.

### 5. Control-plane plumbing (no policy enforcement on these)

- `agent → orchestrator :7000` (registration, policy pull via
  replay, chat relay). `NO_PROXY` contains `orchestrator` so these
  calls go direct, not through Envoy. These are control, not data.
- `orchestrator → policy-processor :8082` (`PUT /config/agents/{id}`
  and `PUT /config/tokens/{id}`). Direct HTTP, no enforcement needed —
  this *is* the enforcement configuration channel.
- `policy-processor → envoy` does not exist as a client direction;
  Envoy is the client of the policy-processor via gRPC.

---

## Identity model

Three traffic types, three rules. ext_proc's `extractIDs` implements
them as a priority chain; the relevant call site decides whether
falling back is acceptable.

| Traffic | Primary identity           | Fallback      | Header on the wire                     |
| ------- | -------------------------- | ------------- | -------------------------------------- |
| A2A     | `Authorization: Bearer`    | *none*        | Bearer only (no `x-agent-id`)          |
| MCP     | `x-agent-id`               | *none*        | `x-agent-id`                           |
| LLM     | `x-agent-id`               | *none*        | `x-agent-id` + openai's Bearer api_key |

`extractIDs` code walks the headers once and returns `(callerID,
targetID, bearerState)`:

- Bearer present + resolvable → `bs=bearerResolved`, `callerID` from token
- Bearer present + unresolvable → `bs=bearerInvalid`, `callerID` falls back to `x-agent-id`
- No Bearer → `bs=bearerAbsent`, `callerID` from `x-agent-id` or `"unknown"`

Then the per-traffic dispatcher in `processor.go` gates:

- A2A branch requires `bs == bearerResolved`; otherwise `invalid-bearer`
  or `missing-bearer`.
- MCP and LLM branches ignore `bearerState` and use `callerID` as-is.
  This is what lets the openai SDK's `Authorization: Bearer sk-...`
  pattern coexist with A2A bearer enforcement.

Tokens themselves are opaque 32-byte hex strings minted by the
orchestrator at first registration. The orchestrator caches
`agent_id → token` (in-memory, replay-on-restart) and pushes to the
policy-processor via `PUT /config/tokens/{id}`. The processor stores
both directions (`tokens.Store` has `byAgent` and `byToken` maps) so
rotation can drop old tokens atomically.

---

## Envoy virtual hosts + clusters

Listener: `0.0.0.0:10000` (HTTP). HTTP Connection Manager runs two
filters: `envoy.filters.http.ext_proc` then `envoy.filters.http.router`.

| Virtual host name             | Domains matched                                                | Cluster                | Upstream                                 |
| ----------------------------- | -------------------------------------------------------------- | ---------------------- | ---------------------------------------- |
| `agent_a`                     | `agent-a`, `agent-a:9002`                                      | `agent_a_cluster`      | `agent-a:9002`                           |
| `agent_b`                     | `agent-b`, `agent-b:9002`                                      | `agent_b_cluster`      | `agent-b:9002`                           |
| `mcp_5_tools`                 | `mcp-5-tools`, `mcp-5-tools:8000`                              | `mcp_5_tools_cluster`  | `mcp-5-tools:8000`                       |
| `litellm`                     | `litellm`, `litellm:4000`                                      | `litellm_cluster`      | `litellm:4000`                           |
| `partner_agent_example_com`   | `partner-agent.example.com`, `partner-agent.example.com:80`    | `a2a_proxy_cluster`    | `a2a-proxy:8082`                         |

All clusters are `STRICT_DNS` with 1 s refresh so newly started
containers are picked up quickly. `connect_timeout` ranges from 5 s
(internal) to 10 s (litellm, which boots slowly). `response_body_mode:
BUFFERED` is on globally so ext_proc sees full response bodies for
token extraction and stats timing.

---

## How HTTPS is unwrapped (for observability)

Three strategies, one per external target class:

1. **api.openai.com / api.anthropic.com (Slice 3)** — LiteLLM sidecar
   is the TLS client. Agents send plain HTTP to `Host: litellm`;
   Envoy forwards plain HTTP to the LiteLLM container; LiteLLM reads
   the real provider from the OpenAI-formatted request
   (e.g. `model: gpt-4o-mini`), opens TLS, and returns plaintext.
   ext_proc sees both request and response plaintext.

2. **Cross-org A2A `https://partner…` (Slice 5)** — a2a-proxy sidecar
   is the TLS client. Agents send plain HTTP with the real partner
   FQDN in `Host`. Envoy matches that FQDN as a virtual host, routes
   plain HTTP to the a2a-proxy; the sidecar reads `Host`, opens TLS
   to the FQDN (using the default HTTPS port 443), and returns
   plaintext.

3. **Internal-only services (MCP, agent A2A)** — stay on plain HTTP
   inside the compose network. mTLS terminated by Envoy is on the
   roadmap but not in Sprint 8.

Outbound `https://api.openai.com/v1/...` via `HTTPS_PROXY=envoy`
*would* be a CONNECT tunnel that blinds Envoy. The patched SDKs avoid
that by rewriting `base_url` to plain HTTP; the a2a-proxy avoids it by
having the `/cross-org-a2a` helper (and eventually the patched A2A
SDK) rewrite `https://` → `http://` before calling.

---

## Threat model — where the trust boundary really is

The enforcement design assumes **every agent-originated request passes
through Envoy**. Identity decisions (A2A bearer resolution, `x-amaze-caller`
injection) and policy decisions (allowlists, rate limits, token budgets)
all happen at that one hop. The SDK on the receiving end trusts the
`x-amaze-caller` header because it's minted by ext_proc after the
bearer has been authenticated.

Docker's default bridge network (used by `docker-compose.yml`) does
**not** enforce that assumption at the network layer. Every container
on `amaze-net` can reach every other container's exposed ports
directly — including the agent's `:9002` A2A port. That means a
compromised peer (or a mis-configured client deliberately bypassing
Envoy) can `POST http://sdk-agent-b:9002/` with any `x-amaze-caller`
value it likes, and the SDK has no way to distinguish that from a
legitimate ext_proc-injected value.

What this means concretely:

- **Against in-band spoof attempts through Envoy** (e.g. forged
  `params.from` in JSON-RPC bodies, pre-set `x-amaze-caller` on
  requests that *do* pass through Envoy), the Sprint 9 fix is
  sufficient — ext_proc overwrites the header with the authenticated
  value. ST-9.3b proves this.

- **Against a hostile container on `amaze-net`** bypassing Envoy
  entirely, the current design offers no defence. A compromise at
  that layer bypasses the control plane entirely — not just
  `x-amaze-caller`, every protection.

Mitigation paths, in order of effort:

1. **Network segmentation.** Deploy with a topology where the agent's
   `:9002` is reachable only from Envoy. Docker compose won't help;
   Kubernetes NetworkPolicy or per-container firewalls will.
2. **HMAC-signed `x-amaze-caller`.** Mint a per-agent secret at
   container start, share it with ext_proc, have the SDK verify the
   signature. Peer bypass can't forge without the secret. Sprint 10.
3. **mTLS between Envoy and agents.** Cert-pinned origin proof. Also
   Sprint 10+ work and the "right" answer long-term.

Until one of those lands, `x-amaze-caller` is trustworthy *iff* the
operator has arranged the network so every caller must route through
Envoy. The SDK's docstring states this inline; operators should also
document it in their own deployment runbooks.

---

## Known limitations (carry-forward)

- a2a-proxy forwards the agent's internal A2A bearer straight to the
  partner. Real deployments must strip it and inject a partner-specific
  credential. Sprint 9+.
- a2a-proxy demo runs with `A2A_PROXY_INSECURE_TLS=true` (accepts
  self-signed). Prod mounts the partner's CA bundle and sets the env
  to `false`.
- MCP still uses `x-agent-id` with no cryptographic binding. When the
  MCP spec gains an auth standard, mirror the A2A bearer treatment.
- Orchestrator mutating endpoints (`POST /agents/register`,
  `PUT /agents/{id}/policy`, `PUT /config/agents/{id}`,
  `PUT /config/tokens/{id}`) are unauthenticated. Safe today only
  because nothing binds them to 0.0.0.0 outside the compose host.
  Sprint 10 hardens this.
- Envoy admin (`:9901`) is on `0.0.0.0` inside compose — restrict or
  disable before moving off a single-host deploy.
- The overload_manager caps downstream connections at 50 000. That's
  a per-Envoy ceiling, not a per-agent one; per-agent rate limits live
  in the policy processor.

---

## Quick reference — "how do I trace request X?"

1. Find the request in Envoy's access log:
   ```
   docker compose -f docker/docker-compose.nemo.yml logs envoy | grep <path>
   ```
   Access log format: `[envoy] <status> <details> up=<upstream> flags=<flags> <method> <host><path>`

2. Find the ext_proc decision in the policy-processor log:
   ```
   docker compose -f docker/docker-compose.nemo.yml logs policy-processor | grep <target>
   ```
   Format: `[ext_proc] PASS|DENY  caller=<id>  target=<id>  method=<rpc>  …`

3. Per-agent stats (sliding windows, JSON):
   ```
   curl -s http://127.0.0.1:8081/stats/agents/<id> | jq .
   ```

4. Per-agent policy:
   ```
   curl -s http://127.0.0.1:8082/config/agents/<id> | jq .
   ```

5. A2A token registry (no list endpoint yet — inspect via
   PUT/DELETE with known ids; `GET /config/tokens` is a TODO).
