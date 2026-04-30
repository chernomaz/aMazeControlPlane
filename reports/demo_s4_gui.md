# Sprint S4 — GUI Implementation Demo

**Date:** 2026-04-30
**Sprint goal:** Replace the static `services/ui_mock/index.html` with a real
React GUI that drives the entire control plane end-to-end from the browser.
Fill the read/write API gaps. Land live policy updates without restart.

---

## What ships

A runnable React app at `http://localhost:5173` (production build behind nginx)
with five tabs that all light up against the live backend:

- **LLM Providers** — list LiteLLM models, Add LLM modal with live YAML preview
- **MCP Servers** — list servers + tools, Approve/Reject, Add MCP modal
- **Agents** — three states (pending → approval card / approved-no-policy →
  policy editor / approved-with-policy → dashboard)
- **Traces** — paginated list, click → full-page detail with SVG sequence
  diagram + 15-col edges table + violations + policy snapshot
- **Alerts** — interactive donut by reason, reason pills, filtered records
  table, URL-deep-link from agent dashboard

Plus reusable charting components: `LineChart` (responsive SVG with hover
tracking), `DonutChart` (8-color palette, click-to-filter), `GraphEditor`
(XYFlow with topological layout + finish-node validation).

---

## What changed under the hood

### Policy storage moved to Redis-primary

Pre-S4: `config/policies.yaml` loaded once at boot into per-addon in-memory
caches. Restart required to pick up changes.

S4: `policy:{agent_id}` JSON in Redis is the source of truth. YAML is
read once on first orchestrator boot via `bootstrap_from_yaml()` (idempotent
SETNX — never overwrites Redis values). All three policy consumers
(`enforcer`, `counters`, `graph_enforcer`) refetch per-request — no
in-memory cache. `PUT /policy/{id}` is observable on the next proxy call
without restart.

### Turn semantics corrected

Pre-S4: "turn = session lifetime" — counters and graph state never reset.
For long-running agents this saturated the per-turn budgets and the demo
broke after a few messages.

S4: **Turn = one user message + the agent's full response cycle**, including
A2A peers. `POST /agents/{id}/messages` clears `session:*:total_*`,
`graph:*`, and `trace_context:*` at the start of every call. Each browser-
driven message gets a fresh trace_id and fresh per-turn budgets. Time-
windowed rate limits (`ts:{agent_id}:llm_tokens`) are NOT reset — they
decay by design.

### Human-readable denial translation

When the proxy denies a call, LangChain wraps the 403 in a generic
"unhandled errors in a TaskGroup" exception that hides the actual reason.
Send-message now scans `audit:global` for denials within the call's time
window and returns a structured `denial` field with a humanised reason —
e.g. `"Tool 'dummy_email' on 'demo-mcp' is not in this agent's
policy.allowed_tools"`. If the agent's reply matches the TaskGroup
signature, we replace the reply text with the humanised denial.

Translator covers: tool-not-allowed, agent-not-allowed, llm-not-allowed,
graph_violation, edge_loop_exceeded, budget_exceeded, rate-limit-exceeded,
invalid-bearer, mcp-not-allowed, host-not-allowed, policy-not-found,
redis-unavailable.

### New endpoints (orchestrator)

| Method | Path | Sprint phase | Purpose |
|---|---|---|---|
| GET | /agents | T1.2 | List with state classification |
| GET | /mcp_servers | T1.2 | List with tools + approved flag |
| GET | /llms | T1.2 | List LiteLLM model_list |
| GET | /traces | T1.2 | Paginated trace summaries |
| GET | /traces/{trace_id} | T3.4 | Full trace assembly |
| POST | /agents/{id}/approve | T2.1 | Approval gate |
| POST | /agents/{id}/reject | T2.1 | |
| POST | /mcp_servers/{name}/approve | T2.1 | |
| POST | /mcp_servers/{name}/reject | T2.1 | |
| POST | /mcp_servers | T2.4 | Manual MCP register |
| POST | /llms | T2.3 | Add LiteLLM model_list entry |
| GET | /policy/{id} | T2.2 | Read from Redis |
| PUT | /policy/{id} | T2.2 | Live update, no restart |
| POST | /agents/{id}/messages | T3.3 | Send user prompt to chat port |
| GET | /agents/{id}/stats | T4.2 | Dashboard payload (24-pt time series + KPIs + 4 breakdowns) |
| GET | /alerts | T4.3 | Grouped by reason + filtered records |
| GET | /export | T6.1 | ZIP with traces.json + audit.csv |

---

## How to demo

**Prereq:** the platform is already running (`docker compose up`); agent-sdk
needs `agent:agent-sdk:chat_endpoint = http://host.docker.internal:8090`
in Redis (manual SET — SDK auto-registration of chat_endpoint is a
follow-up).

```bash
# Frontend
cd services/ui && npm run dev    # http://localhost:3000 (dev) or
                                 # http://localhost:5173 (compose nginx)
```

Then in the browser:

1. **Agents tab** → click `agent-sdk` row (state=approved-with-policy) →
   dashboard renders with KPIs (176 calls in 24h), line chart (Calls/Latency
   toggle), 4 donuts.
2. Send a message via the dashboard composer: *"search for current weather
   in London"* → agent replies (real Tavily web_search via demo-mcp), trace
   row appears in the Recent Traces table within seconds.
3. Click the trace → full-page detail with sequence diagram (User →
   openai/gpt-4.1-mini → web_search → agent-sdk2 → ... → response), 15-col
   edges table, policy snapshot.
4. Send a non-weather prompt: *"check my email"* → agent's reply replaced
   with `"Denied: Tool 'dummy_email' on 'demo-mcp' is not in this agent's
   policy.allowed_tools"`. Trace shows the denial.
5. **Agents tab** → click agent row → policy editor → toggle Strict mode
   → graph canvas appears → drag-to-connect web_search → finish → Save.
   Next call enforces the new graph (no restart).
6. **Alerts tab** → big donut shows tool-not-allowed (3) +
   rate-limit-exceeded (2) etc. Click a slice → filtered records table.
   Row click → trace detail.
7. **Traces tab** → "Export" button → modal → date range, agent filter,
   content checkboxes → Download → ZIP with traces.json + audit.csv.

---

## Verification

| Test suite | Result |
|---|---|
| `pytest tests/test_s4.py` | 17 passed, 2 skipped |
| `pytest tests/test_s4_export.py` | 8 passed |
| `pytest tests/test_s3.py` | regression-clean (covered by autouse fixture compatibility) |
| `npm run build` (services/ui) | clean, 220 kB gzip JS |

Skipped: ST-S4.18 (alerts donut UI integration — needs browser harness)
and a duplicate ST-S4.19 stub (export covered fully by `test_s4_export.py`).

---

## Known follow-ups

1. **SDK chat_endpoint registration** — currently must be SET manually in
   Redis for any agent that supports user-driven send-message. SDK should
   auto-register on `Config.register()`. Tracked.
2. **Latency time series** — `kpi.avg_latency_ms` is `None` until the
   proxy emits a `ts:{agent_id}:llm_latency_ms` series. Counter writer
   addition needed.
3. **HTML sequence-diagram export** — UI checkbox exists but disabled
   ("S5+"). Backend export currently ships traces.json + audit.csv only.
4. **Multi-tenant turn-reset** — current send-message resets ALL session
   counters and graph state, which is correct for single-conversation
   demos. Multi-tenant needs conversation-scoped state instead.
5. **Tool-advertisement filter** — TODO.md #1 still parked; not in S4
   scope.

---

## Sprint timing

26 tasks across 6 phases, parallelised aggressively:

| Phase | Wall clock | Tasks |
|---|---|---|
| 1 — Foundation | ~10 min | 5 (T1.0 sign-off + 4 parallel) |
| 2 — Approvals + policy persist | ~3.5 min | 7 (4 backend + 3 UI parallel) |
| 3 — Editor + send + trace | ~5 min + ~5 min tests | 6 |
| 4 — Stats backend | ~2 min | 3 (parallel) |
| 5 — Charts + alerts UI | ~4.5 min | 4 (parallel) |
| 6 — Export | ~3 min | 3 (parallel) + tests |

Total agent wall-clock for ~25 implementation tasks: ~35 minutes.

---

## Sprint S4: COMPLETE
