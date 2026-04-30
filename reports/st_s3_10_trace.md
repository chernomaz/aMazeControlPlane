# ST-S3.10 — Trace Log: NY News MCP Tool Deny

## Trace Metadata

```
trace_id      : 482263540ac46121fcaab9ecd72aca45
jaeger        : http://localhost:16686/trace/482263540ac46121fcaab9ecd72aca45
total spans   : 7
business spans: 2  (LLM ×1, denied MCP tool ×1)
proto spans   : 5  (MCP protocol negotiation — not in audit log)
wall clock    : ~1.7 s  (denial is near-instant)
```

> **Note:** All three ST-S3.x integration tests share the same `trace_id` because
> agent-sdk reuses its session across requests in this run.

---

## Flow Narrative

```
+0 ms    agent-sdk LLM #1  — "email me the current NEW YORK news"
                             model=gpt-4.1-mini
                             System prompt says 'web_search only' but LLM
                             decides to call dummy_email for the email request.
                             → tool_calls: [dummy_email({"person":"alice"})]
                             (indirect=true)

+1653 ms MCP session setup — 5 protocol spans
         (initialize, notifications, tools/list, SSE stream open)

+1695 ms MCP dummy_email  — tools/call → DENIED immediately (0.9 ms)
         proxy returns 403: {"error":"denied","reason":"tool-not-allowed",
                             "server":"demo-mcp","tool":"dummy_email"}
         alert written to audit log

         LangChain TaskGroup raises exception from the 403.
         agent-sdk catches it: return f"Error: {e}"
         Routing to agent-sdk2 never happens.

+1696 ms (trace ends — no A2A, no agent-sdk2 LLM)
```

---

## Span Summary Table

| # | Span ID          | Offset    | Duration  | Kind      | Agent      | Method | Target          | Tool       | Audit |
|---|------------------|-----------|-----------|-----------|------------|--------|-----------------|------------|-------|
| 1 | ec542b01c05571e4 | +    0ms  |  1639.5ms | llm       | agent-sdk  | POST   | api.openai.com  |            | ✓     |
| 2 | f17cc7f00adbc37d | + 1653ms  |     4.1ms | mcp       | agent-sdk  | POST   | demo-mcp        |            | —     |
| 3 | d6fb44bf968853bf | + 1661ms  |     3.8ms | mcp       | agent-sdk  | POST   | demo-mcp        |            | —     |
| 4 | bcbd9d746e980e5d | + 1676ms  |     5.6ms | mcp       | agent-sdk  | POST   | demo-mcp        |            | —     |
| 5 | 829b1f3dbb0e0092 | + 1676ms  |     4.7ms | mcp       | agent-sdk  | GET    | demo-mcp        |            | —     |
| 6 | 7269625a76120836 | + 1687ms  |     2.5ms | mcp       | agent-sdk  | POST   | demo-mcp        |            | —     |
| 7 | 91c979bfc42a685b | + 1695ms  |     0.9ms | mcp       | agent-sdk  | POST   | demo-mcp        |            | ✓     |

> Protocol spans (—) = MCP transport negotiation; in Jaeger but not in audit log.

---

## Span 1 — agent-sdk LLM #1 (planner)

```
span_id   : ec542b01c05571e4
offset    : +0 ms
duration  : 1639.5 ms
kind      : llm
agent     : agent-sdk
provider  : openai
model     : gpt-4.1-mini
indirect  : true   (response contains tool_calls)
```

**Response tool call**
```json
{
  "tool_calls": [{
    "function": {
      "name": "dummy_email",
      "arguments": "{\"person\":\"alice\"}"
    }
  }]
}
```

---

## Spans 2–6 — MCP Protocol Setup (not in audit)

```
spans  : f17cc7f0, d6fb44bf, bcbd9d74, 829b1f3d, 72696258
offset : +1653 ms → +1689 ms
agent  : agent-sdk
target : demo-mcp
```

Initialize + notifications + tools/list + GET SSE — normal MCP handshake.

---

## Span 7 — agent-sdk MCP dummy_email (DENIED)

```
span_id       : 91c979bfc42a685b
offset        : +1695 ms
duration      : 0.9 ms   ← denied before reaching upstream
kind          : mcp      (fixed in enforcer.py — kind set before deny)
agent         : agent-sdk
target        : demo-mcp
denied        : true
denial_reason : tool-not-allowed
```

**Request**
```json
{
  "method": "tools/call",
  "params": {"name": "dummy_email", "arguments": {"person": "alice"}},
  "jsonrpc": "2.0",
  "id": 1
}
```

**Response (from proxy — upstream never contacted)**
```json
{
  "error": "denied",
  "reason": "tool-not-allowed",
  "server": "demo-mcp",
  "tool": "dummy_email"
}
```

**Alert written to audit log**
```json
{
  "type": "tool-not-allowed",
  "kind": "mcp",
  "agent_id": "agent-sdk",
  "target": "demo-mcp",
  "server": "demo-mcp",
  "tool": "dummy_email"
}
```

---

## Jaeger UI

```
http://localhost:16686/trace/482263540ac46121fcaab9ecd72aca45
```

7 spans visible. The denial at +1695 ms terminates the trace immediately —
no A2A hop, no agent-sdk2 spans.
