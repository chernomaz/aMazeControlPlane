# ST-S3.9 — Trace Log: Weather A2A + LLM + MCP

## Trace Metadata

```
trace_id      : 482263540ac46121fcaab9ecd72aca45
jaeger        : http://localhost:16686/trace/482263540ac46121fcaab9ecd72aca45
total spans   : 39
business spans: 9  (LLM ×4, MCP tool ×4, A2A ×1)
proto spans   : 30 (MCP protocol negotiation — not in audit log)
wall clock    : ~9.6 s
```

> **Note:** All three ST-S3.x integration tests share the same `trace_id` because
> agent-sdk reuses its session across requests in this run.

---

## Flow Narrative

```
+0 ms      agent-sdk LLM #1  — "search for current weather in London"
                               model=gpt-4.1-mini; 305 prompt tokens
                               → tool_calls: [web_search("current weather in London")]
                               (indirect=true)

+1310 ms   agent-sdk MCP session setup — 6 protocol spans
           (initialize, notifications, tools/list, SSE stream open)

+1346 ms   agent-sdk MCP web_search #1 — 202 Accepted (3ms, no output)
+1353 ms   agent-sdk MCP web_search #2 — SSE result (1688ms)
           → London weather: sunny 16.2°C, ENE wind 16.6 mph, humidity 34%

+3050 ms   agent-sdk MCP teardown — 4 protocol spans

+3088 ms   agent-sdk LLM #2  — synthesizes web_search result
                               has_tool_calls_input=true; 1052 prompt tokens
                               → "The current weather in London is sunny with
                                  a temperature of 16.2°C …"

+5312 ms   A2A agent-sdk → agent-sdk2 (connection held open)
           tasks/send: forwards synthesized weather answer

+5326 ms     agent-sdk2 MCP init — _build_agent() loads tools from demo-mcp
             (10 protocol spans including tools/list — first call to agent-sdk2)

+5430 ms     agent-sdk2 LLM #1 — receives weather message, detects "weather"
                                   model=gpt-4.1-mini; 338 prompt tokens
                                   → tool_calls: [web_search("current weather
                                      in London, UK")]
                                   (indirect=true)

+6453 ms     agent-sdk2 MCP session setup — 6 protocol spans for tool call

+6500 ms     agent-sdk2 MCP web_search #1 — 202 Accepted (3ms)
+6507 ms     agent-sdk2 MCP web_search #2 — SSE result (1923ms)
             → London weather data (slightly different wording from Tavily)

+8436 ms     agent-sdk2 MCP teardown — 4 protocol spans

+8472 ms     agent-sdk2 LLM #2 — synthesizes result
                                   has_tool_calls_input=true; 1067 prompt tokens
                                   → "The current weather in London, UK is
                                      sunny with a temperature of 15.3°C …"

+9646 ms   A2A response returns to agent-sdk (span 15 closes)
           artifact text = agent-sdk2 LLM #2 output
```

---

## Span Summary Table

| # | Span ID          | Offset    | Duration  | Kind       | Agent      | Method | Target           | Tool       | Audit |
|---|------------------|-----------|-----------|------------|------------|--------|------------------|------------|-------|
| 1 | b409b82ba3263380 | +    0ms |   1297.8ms | llm        | agent-sdk  | POST   | api.openai.com  |            | ✓ |
| 2 | 232c43bf78ac60ab | + 1310ms |      4.4ms | mcp        | agent-sdk  | POST   | demo-mcp        |            | — |
| 3 | 16fd9111df956707 | + 1317ms |      3.0ms | mcp        | agent-sdk  | POST   | demo-mcp        |            | — |
| 4 | a5723b3d264b425b | + 1329ms |      5.1ms | mcp        | agent-sdk  | POST   | demo-mcp        |            | — |
| 5 | 452017177c09b862 | + 1329ms |      4.1ms | mcp        | agent-sdk  | GET    | demo-mcp        |            | — |
| 6 | db1739c2c795d052 | + 1338ms |      2.2ms | mcp        | agent-sdk  | POST   | demo-mcp        |            | — |
| 7 | b9f3fe4dab12adf0 | + 1338ms |   1742.4ms | mcp        | agent-sdk  | GET    | demo-mcp        |            | — |
| 8 | 76605f82b8975853 | + 1346ms |      3.1ms | mcp        | agent-sdk  | POST   | demo-mcp        | web_search | ✓ |
| 9 | 070987708920764c | + 1353ms |   1688.3ms | mcp        | agent-sdk  | POST   | demo-mcp        | web_search | ✓ |
|10 | 955c759b4bcd902e | + 3050ms |      4.7ms | mcp        | agent-sdk  | POST   | demo-mcp        |            | — |
|11 | 2bb4dff1d33e8cd3 | + 3059ms |      5.3ms | mcp        | agent-sdk  | POST   | demo-mcp        |            | — |
|12 | c603796d50e9d80b | + 3072ms |      3.3ms | mcp        | agent-sdk  | DELETE | demo-mcp        |            | — |
|13 | 97ff421d74a5e83a | + 3078ms |      1.9ms | mcp        | agent-sdk  | DELETE | demo-mcp        |            | — |
|14 | ed9b073f73c390b3 | + 3088ms |   2213.3ms | llm        | agent-sdk  | POST   | api.openai.com  |            | ✓ |
|15 | 127844624afaa039 | + 5312ms |   4334.0ms | a2a        | agent-sdk  | POST   | agent-sdk2      |            | ✓ |
|16 | 3cf6b2fea3a87454 | + 5326ms |      3.2ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|17 | fe4c7c5a73c97109 | + 5333ms |      4.7ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|18 | e5488e1de0b7a874 | + 5346ms |      4.1ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|19 | 49b3a429d3cf39d7 | + 5346ms |      4.4ms | mcp        | agent-sdk2 | GET    | demo-mcp        |            | — |
|20 | c8bb52bf8ad35751 | + 5356ms |     30.9ms | mcp        | agent-sdk2 | GET    | demo-mcp        |            | — |
|21 | a622fea855c5fa0d | + 5356ms |      3.1ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|22 | e859c804d658613e | + 5364ms |      3.1ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|23 | 97e94dfe6cf65cff | + 5370ms |      3.3ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|24 | 6cc0c7e019ec86c9 | + 5379ms |      2.9ms | mcp        | agent-sdk2 | DELETE | demo-mcp        |            | — |
|25 | 0cce91c36fc9f70e | + 5385ms |      1.8ms | mcp        | agent-sdk2 | DELETE | demo-mcp        |            | — |
|26 | 4fbde8d949166aa2 | + 5430ms |   1006.1ms | llm        | agent-sdk2 | POST   | api.openai.com  |            | ✓ |
|27 | f2781f2d66911e2c | + 6453ms |      4.4ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|28 | 8aea0ec273f4058d | + 6461ms |      3.2ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|29 | 61fd86d84810d927 | + 6473ms |     10.2ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|30 | 3e68606210d7488e | + 6473ms |      6.1ms | mcp        | agent-sdk2 | GET    | demo-mcp        |            | — |
|31 | e6c8167956083913 | + 6488ms |   1976.3ms | mcp        | agent-sdk2 | GET    | demo-mcp        |            | — |
|32 | 9d7f6496c08fc47e | + 6489ms |      4.3ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|33 | 6b4bd3c066e87809 | + 6500ms |      3.2ms | mcp        | agent-sdk2 | POST   | demo-mcp        | web_search | ✓ |
|34 | fe0e17a49ae5001e | + 6507ms |   1922.9ms | mcp        | agent-sdk2 | POST   | demo-mcp        | web_search | ✓ |
|35 | 17975d6629bd6925 | + 8436ms |      3.7ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|36 | 7a4ceb7a623fed4a | + 8443ms |      3.1ms | mcp        | agent-sdk2 | POST   | demo-mcp        |            | — |
|37 | 14029302278ed6a6 | + 8453ms |      3.8ms | mcp        | agent-sdk2 | DELETE | demo-mcp        |            | — |
|38 | f9e483cee8ae5992 | + 8461ms |      3.1ms | mcp        | agent-sdk2 | DELETE | demo-mcp        |            | — |
|39 | 57892f98bfcc41b4 | + 8472ms |   1168.3ms | llm        | agent-sdk2 | POST   | api.openai.com  |            | ✓ |

> Protocol spans (—) = MCP transport negotiation; in Jaeger but not in audit log.

---

## Audit ↔ Span Cross-Reference

| Audit # | Span ID          | Kind      | Agent      | Tool       | Indirect | ToolInput |
|---------|------------------|-----------|------------|------------|----------|-----------|
| 1       | b409b82ba3263380 | llm       | agent-sdk  | —          | true     | false     |
| 2       | 76605f82b8975853 | mcp       | agent-sdk  | web_search | —        | —         |
| 3       | 070987708920764c | mcp       | agent-sdk  | web_search | —        | —         |
| 4       | ed9b073f73c390b3 | llm       | agent-sdk  | —          | false    | true      |
| 5       | 127844624afaa039 | a2a       | agent-sdk  | —          | —        | —         |
| 6       | 4fbde8d949166aa2 | llm       | agent-sdk2 | —          | true     | false     |
| 7       | 6b4bd3c066e87809 | mcp       | agent-sdk2 | web_search | —        | —         |
| 8       | fe0e17a49ae5001e | mcp       | agent-sdk2 | web_search | —        | —         |
| 9       | 57892f98bfcc41b4 | llm       | agent-sdk2 | —          | false    | true      |

---

## Token Summary

| Agent      | Call   | Prompt | Completion | Total |
|------------|--------|--------|------------|-------|
| agent-sdk  | LLM #1 | 305    | 17         | 322   |
| agent-sdk  | LLM #2 | 1052   | 82         | 1134  |
| agent-sdk2 | LLM #1 | 338    | 19         | 357   |
| agent-sdk2 | LLM #2 | 1067   | 55         | 1122  |
| **Total**  |        | **2762** | **173**  | **2935** |

---

## Jaeger UI

```
http://localhost:16686/trace/482263540ac46121fcaab9ecd72aca45
```