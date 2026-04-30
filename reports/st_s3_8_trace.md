# ST-S3.8 — Trace Log: Bitcoin A2A + LLM + MCP

## Trace Metadata

```
trace_id : 482263540ac46121fcaab9ecd72aca45
jaeger   : http://localhost:16686/trace/482263540ac46121fcaab9ecd72aca45
session  : agent-sdk  → 6be4a8a4-0671-4880-b356-ffdf0ec3b49d
           agent-sdk1 → (own root span, same trace via traceparent propagation)
total spans   : 16
business spans: 6  (LLM ×3, MCP tool ×2, A2A ×1)
proto spans   : 10 (MCP protocol negotiation — not in audit log)
wall clock    : ~8.3 s  (+0 ms → +8349 ms)
```

---

## Flow Narrative

```
+0 ms      agent-sdk LLM #1  — user asks "search for current bitcoin price"
                               model=gpt-4.1-mini; 304 prompt tokens
                               → tool_calls: [web_search("current bitcoin price")]
                               (indirect=true; audit records this)

+1875 ms   MCP session setup  — agent-sdk connects to demo-mcp MCP server
                               spans 2–7: initialize, notifications, tools/list,
                               resources/list, prompts/list, SSE stream open
                               (6 protocol spans; NOT in audit log)

+1923 ms   MCP web_search #1  — tools/call → web_search("current bitcoin price")
                               immediate 202 Accepted; result pending on SSE
                               (audit record; 0 chars output)

+1930 ms   MCP web_search #2  — tools/call → web_search("current bitcoin price")
                               1399 ms → SSE result with BTC price data
                               (audit record; 1998 chars output)

+3338 ms   MCP teardown       — spans 10–13: post-call list + DELETE sessions
                               (4 protocol spans; NOT in audit log)

+3430 ms   agent-sdk LLM #2   — model synthesizes tool results into answer
                               model=gpt-4.1-mini; 700 prompt tokens
                               has_tool_calls_input=true
                               → "The current price of Bitcoin is approximately
                                  $77,007.86 according to Coinbase …"

+5877 ms   A2A agent-sdk → agent-sdk1 (span 15 opens, HTTP connection held open)
                               JSON-RPC tasks/send carrying synthesized BTC price
                               method: tasks/send  id: task-sdk-1
                               (trace propagated via traceparent header)

+5912 ms     agent-sdk1 LLM (span 16) — agent-sdk1 receives task, calls own LLM
                               model=gpt-4.1-mini; 342 prompt tokens
                               → "Thank you for the update on the current price
                                  of Bitcoin …"
                               ← LLM answer packed into tasks/send artifact

+8349 ms   A2A response returns to agent-sdk (span 15 closes)
           span 15 output artifact = span 16 LLM output (same text)
```

---

## Span Summary Table

| # | Span ID          | Offset    | Duration  | Kind       | Agent      | Method | Target           | Tool       | In Audit |
|---|------------------|-----------|-----------|------------|------------|--------|------------------|------------|----------|
| 1 | 630b380c6c0a6e90 | +0 ms     | 1770.4 ms | llm        | agent-sdk  | POST   | api.openai.com   |            | ✓        |
| 2 | a91e66ad1f9e114e | +1875 ms  | 4.0 ms    | mcp-proto  | agent-sdk  | POST   | demo-mcp         |            | —        |
| 3 | 9fb176033514b381 | +1882 ms  | 3.5 ms    | mcp-proto  | agent-sdk  | POST   | demo-mcp         |            | —        |
| 4 | 3e7df2471cad871b | +1899 ms  | 8.8 ms    | mcp-proto  | agent-sdk  | GET    | demo-mcp         |            | —        |
| 5 | 9fdb4dfef82081a0 | +1900 ms  | 7.9 ms    | mcp-proto  | agent-sdk  | POST   | demo-mcp         |            | —        |
| 6 | 693f89bfaa40bf14 | +1914 ms  | 2.7 ms    | mcp-proto  | agent-sdk  | POST   | demo-mcp         |            | —        |
| 7 | ed0cbcd60a760ffe | +1914 ms  | 1506.2 ms | mcp-proto  | agent-sdk  | GET    | demo-mcp         |            | —        |
| 8 | db1732381b922215 | +1923 ms  | 3.1 ms    | mcp-tool   | agent-sdk  | POST   | demo-mcp         | web_search | ✓        |
| 9 | c4fb81f42f94429e | +1930 ms  | 1398.5 ms | mcp-tool   | agent-sdk  | POST   | demo-mcp         | web_search | ✓        |
|10 | 8be90fe31c315201 | +3338 ms  | 4.1 ms    | mcp-proto  | agent-sdk  | POST   | demo-mcp         |            | —        |
|11 | a5be49e88f1e1a6f | +3346 ms  | 4.4 ms    | mcp-proto  | agent-sdk  | POST   | demo-mcp         |            | —        |
|12 | 1dab989a3922aa1d | +3410 ms  | 3.6 ms    | mcp-proto  | agent-sdk  | DELETE | demo-mcp         |            | —        |
|13 | d8a97f7162321398 | +3418 ms  | 2.4 ms    | mcp-proto  | agent-sdk  | DELETE | demo-mcp         |            | —        |
|14 | 7b1495de4b018897 | +3430 ms  | 2436.1 ms | llm        | agent-sdk  | POST   | api.openai.com   |            | ✓        |
|15 | 1376b0885696d78f | +5877 ms  | 2472.1 ms | a2a        | agent-sdk  | POST   | agent-sdk1       |            | ✓        |
|16 | 6f3e61b5195750ab | +5912 ms  | 2432.1 ms | llm        | agent-sdk1 | POST   | api.openai.com   |            | ✓        |

> **Protocol spans (—)** are present in Jaeger but filtered from the audit log by design.
> They represent MCP transport negotiation (initialize, capability lists, SSE, teardown).

---

## Business Span Detail

### Span 1 — agent-sdk LLM #1 (planner)

```
span_id   : 630b380c6c0a6e90
parent    : cfa1b9b175816c2d
offset    : +0 ms
duration  : 1770.4 ms
kind      : llm
agent     : agent-sdk
provider  : openai
model     : gpt-4.1-mini
indirect  : true   (response contains tool_calls → not the final answer)
```

**Request (key fields)**
```json
{
  "model": "gpt-4.1-mini",
  "messages": [
    {"role": "system", "content": "You are a helpful research assistant. Use the web_search tool ONLY when you need up-to-date external facts…"},
    {"role": "user",   "content": "search for current bitcoin price"}
  ],
  "tools": ["sql_query", "dummy_email", "web_search", "file_read"]
}
```

**Response (key fields)**
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_BRqxE0Npk2OfMN8TABDyVikH",
        "type": "function",
        "function": {
          "name": "web_search",
          "arguments": "{\"query\":\"current bitcoin price\"}"
        }
      }]
    }
  }],
  "usage": {"prompt_tokens": 304, "completion_tokens": 16, "total_tokens": 320}
}
```

---

### Spans 2–7 — MCP Protocol Negotiation (not in audit)

```
spans     : a91e66ad, 9fb17603, 3e7df247, 9fdb4dfe, 693f89bf, ed0cbcd6
offset    : +1875 ms → +1914 ms
agent     : agent-sdk
target    : demo-mcp
```

MCP HTTP+SSE transport handshake: `POST /` initialize → `POST /` notifications/initialized →
`GET /sse` capability poll → `POST /` tools/list + resources/list + prompts/list →
`GET /sse` long-lived SSE stream (1506 ms, holds open for result delivery).
These spans are recorded in Jaeger for transport-level visibility but excluded from the
audit log — they carry no business payload.

---

### Span 8 — agent-sdk MCP web_search #1 (request acknowledged)

```
span_id   : db1732381b922215
parent    : cfa1b9b175816c2d
offset    : +1923 ms
duration  : 3.1 ms
kind      : mcp / tools/call
agent     : agent-sdk
target    : demo-mcp
tool      : web_search
denied    : false
```

**Request**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "web_search",
    "arguments": {"query": "current bitcoin price"}
  }
}
```

**Response**
```
(empty — MCP server returned 202 Accepted; result delivered via SSE stream on span 7)
```

---

### Span 9 — agent-sdk MCP web_search #2 (result received)

```
span_id   : c4fb81f42f94429e
parent    : cfa1b9b175816c2d
offset    : +1930 ms
duration  : 1398.5 ms
kind      : mcp / tools/call
agent     : agent-sdk
target    : demo-mcp
tool      : web_search
denied    : false
```

**Request**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "web_search",
    "arguments": {"query": "current bitcoin price"}
  }
}
```

**Response (first 500 chars)**
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"_meta":{"fastmcp":{"wrap_result":true}},
      "content":[{"type":"text","text":"Title: Bitcoin (BTC) Price USD Today,
      News, Charts, Market Cap | CoinMarketCap\nLink: https://coinmarketcap.com/…
      \n\nTitle: Bitcoin Price | BTC Price Index and Live Chart - CoinDesk\n…
      Price: $77,007.86 USD …"}]}}
```

---

### Spans 10–13 — MCP Session Teardown (not in audit)

```
spans     : 8be90fe3, a5be49e8, 1dab989a, d8a97f71
offset    : +3338 ms → +3420 ms
agent     : agent-sdk
target    : demo-mcp
```

Post-call cleanup: `POST /` (list calls) + `DELETE /` × 2 (session close).
Excluded from audit log.

---

### Span 14 — agent-sdk LLM #2 (synthesizer)

```
span_id             : 7b1495de4b018897
parent              : cfa1b9b175816c2d
offset              : +3430 ms
duration            : 2436.1 ms
kind                : llm
agent               : agent-sdk
provider            : openai
model               : gpt-4.1-mini
indirect            : false  (response is the final answer)
has_tool_calls_input: true   (messages include role=tool result from web_search)
```

**Request (key fields)**
```
model     : gpt-4.1-mini
messages  : system + user("bitcoin price") + assistant(tool_call) + tool(web_search result)
```

**Response (key fields)**
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "The current price of Bitcoin is approximately $77,007.86 according to Coinbase. Other sources show slightly varying prices around $76,355 to $77,000."
    }
  }],
  "usage": {"prompt_tokens": 700, "completion_tokens": 35, "total_tokens": 735}
}
```

---

### Span 15 — A2A agent-sdk → agent-sdk1 (request)

```
span_id   : 1376b0885696d78f
parent    : cfa1b9b175816c2d
offset    : +5877 ms
kind      : a2a
agent     : agent-sdk
target    : agent-sdk1
denied    : false
```

agent-sdk sends the synthesized BTC price to agent-sdk1. The HTTP connection stays open —
agent-sdk waits for agent-sdk1 to finish processing before it gets a response back.

**Request**
```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "tasks/send",
  "params": {
    "id": "task-sdk-1",
    "message": {
      "role": "user",
      "parts": [{
        "type": "text",
        "text": "The current price of Bitcoin is approximately $77,007.86 according to Coinbase. Other sources show slightly varying prices around $76,355 to $77,000."
      }]
    }
  }
}
```

↓ agent-sdk1 receives the task and calls its LLM — see Span 16 below ↓

---

### Span 16 — agent-sdk1 LLM

```
span_id   : 6f3e61b5195750ab
parent    : a0c8a113444f4e45   ← agent-sdk1's own root span
offset    : +5912 ms
duration  : 2432.1 ms
kind      : llm
agent     : agent-sdk1
provider  : openai
model     : gpt-4.1-mini
indirect  : false
has_tool_calls_input: false
```

> parent `a0c8a113444f4e45` is agent-sdk1's local root span. The shared `trace_id` was
> propagated via the `traceparent` header in the A2A request, linking both agents' spans
> into one trace in Jaeger.

**Response**
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "Thank you for the update on the current price of Bitcoin. If you need any specific information or analysis related to Bitcoin or cryptocurrency prices, feel free to ask!"
    }
  }],
  "usage": {"prompt_tokens": 342, "completion_tokens": 33, "total_tokens": 375}
}
```

↑ agent-sdk1 packs this answer into the A2A artifact and returns the HTTP response ↑

---

### Span 15 — A2A agent-sdk → agent-sdk1 (response)

```
span_id   : 1376b0885696d78f  (same span, now closed)
duration  : 2472.1 ms         (35 ms request transit + 2432 ms LLM + 5 ms return)
```

**Response** — artifact contains span 16's LLM output verbatim
```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "id": "task-sdk-1",
    "status": {"state": "completed"},
    "artifacts": [{
      "parts": [{
        "type": "text",
        "text": "Thank you for the update on the current price of Bitcoin. If you need any specific information or analysis related to Bitcoin or cryptocurrency prices, feel free to ask!"
      }]
    }]
  }
}
```

---

## Audit ↔ Span Cross-Reference

| Audit # | Span ID          | Kind      | Agent      | Tool       | Indirect | ToolInput |
|---------|------------------|-----------|------------|------------|----------|-----------|
| 1       | 630b380c6c0a6e90 | llm       | agent-sdk  | —          | true     | false     |
| 2       | db1732381b922215 | mcp       | agent-sdk  | web_search | —        | —         |
| 3       | c4fb81f42f94429e | mcp       | agent-sdk  | web_search | —        | —         |
| 4       | 7b1495de4b018897 | llm       | agent-sdk  | —          | false    | true      |
| 5       | 1376b0885696d78f | a2a       | agent-sdk  | —          | —        | —         |
| 6       | 6f3e61b5195750ab | llm       | agent-sdk1 | —          | false    | false     |

**Columns:**
- `Indirect` — LLM response contained `tool_calls` (model is delegating, not finalising)
- `ToolInput` — LLM request contained `role=tool` messages (model is synthesising from results)

---

## Token Summary

| Agent      | Call          | Prompt | Completion | Total |
|------------|---------------|--------|------------|-------|
| agent-sdk  | LLM #1        | 304    | 16         | 320   |
| agent-sdk  | LLM #2        | 700    | 35         | 735   |
| agent-sdk1 | LLM           | 342    | 33         | 375   |
| **Total**  |               | **1346** | **84**   | **1430** |

---

## Jaeger UI

```
http://localhost:16686/trace/482263540ac46121fcaab9ecd72aca45
```

All 16 spans visible under service `amaze-proxy`. The A2A hop at +5877 ms
visually connects agent-sdk's spans (parent `cfa1b9b175816c2d`) to agent-sdk1's
LLM call (parent `a0c8a113444f4e45`) via the shared trace ID.
