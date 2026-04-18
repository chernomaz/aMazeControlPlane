package stats

import (
	"sync"
	"time"
)

// ── internal data ─────────────────────────────────────────────────────────────

type tokenEntry struct {
	ts     time.Time
	tokens int
}

type agentData struct {
	allowed         int
	denied          int
	totalResponseMs float64
	responseCount   int
	toolCalls       map[string]int
	tokenLog        []tokenEntry
}

type toolData struct {
	calls           int
	totalResponseMs float64
	responseCount   int
}

// ── Collector ─────────────────────────────────────────────────────────────────

type Collector struct {
	mu     sync.Mutex
	agents map[string]*agentData
	tools  map[string]*toolData
}

func NewCollector() *Collector {
	return &Collector{
		agents: make(map[string]*agentData),
		tools:  make(map[string]*toolData),
	}
}

func (c *Collector) getAgent(id string) *agentData {
	if a, ok := c.agents[id]; ok {
		return a
	}
	a := &agentData{toolCalls: make(map[string]int)}
	c.agents[id] = a
	return a
}

func (c *Collector) getTool(name string) *toolData {
	if t, ok := c.tools[name]; ok {
		return t
	}
	t := &toolData{}
	c.tools[name] = t
	return t
}

// RecordRequest is called at the allow/deny decision point in the request phase.
// toolName is non-empty only for allowed MCP tools/call requests.
func (c *Collector) RecordRequest(callerID, toolName string, allowed bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	a := c.getAgent(callerID)
	if allowed {
		a.allowed++
		if toolName != "" {
			a.toolCalls[toolName]++
			c.getTool(toolName).calls++
		}
	} else {
		a.denied++
	}
}

// RecordResponse is called in the response_body phase for allowed requests.
// tokens is 0 for non-LLM responses.
func (c *Collector) RecordResponse(callerID, toolName string, duration time.Duration, tokens int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ms := float64(duration.Milliseconds())
	a := c.getAgent(callerID)
	a.totalResponseMs += ms
	a.responseCount++
	if toolName != "" {
		t := c.getTool(toolName)
		t.totalResponseMs += ms
		t.responseCount++
	}
	if tokens > 0 {
		// Prune entries older than 1 hour (longest window we report) to bound memory.
		cutoff := time.Now().Add(-time.Hour)
		log := a.tokenLog
		start := 0
		for start < len(log) && log[start].ts.Before(cutoff) {
			start++
		}
		if start > 0 {
			trimmed := make([]tokenEntry, len(log)-start)
			copy(trimmed, log[start:])
			log = trimmed
		}
		a.tokenLog = append(log, tokenEntry{ts: time.Now(), tokens: tokens})
	}
}

// ── Snapshots ─────────────────────────────────────────────────────────────────

type AgentSnapshot struct {
	RequestsAllowed int            `json:"requests_allowed"`
	RequestsDenied  int            `json:"requests_denied"`
	AvgResponseMs   float64        `json:"avg_response_time_ms"`
	ToolCalls       map[string]int `json:"tool_calls"`
	TokensPer5Min   int            `json:"tokens_per_5min"`
	TokensPerHour   int            `json:"tokens_per_hour"`
}

type ToolSnapshot struct {
	Calls         int     `json:"calls"`
	AvgResponseMs float64 `json:"avg_response_time_ms"`
}

func avgMs(total float64, count int) float64 {
	if count == 0 {
		return 0
	}
	return total / float64(count)
}

func windowTokens(log []tokenEntry, window time.Duration) int {
	cutoff := time.Now().Add(-window)
	total := 0
	for _, e := range log {
		if e.ts.After(cutoff) {
			total += e.tokens
		}
	}
	return total
}

func toAgentSnapshot(a *agentData) AgentSnapshot {
	toolCalls := make(map[string]int, len(a.toolCalls))
	for k, v := range a.toolCalls {
		toolCalls[k] = v
	}
	return AgentSnapshot{
		RequestsAllowed: a.allowed,
		RequestsDenied:  a.denied,
		AvgResponseMs:   avgMs(a.totalResponseMs, a.responseCount),
		ToolCalls:       toolCalls,
		TokensPer5Min:   windowTokens(a.tokenLog, 5*time.Minute),
		TokensPerHour:   windowTokens(a.tokenLog, time.Hour),
	}
}

func (c *Collector) SnapshotAgent(id string) (AgentSnapshot, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	a, ok := c.agents[id]
	if !ok {
		return AgentSnapshot{}, false
	}
	return toAgentSnapshot(a), true
}

func (c *Collector) SnapshotAgents() map[string]AgentSnapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]AgentSnapshot, len(c.agents))
	for id, a := range c.agents {
		out[id] = toAgentSnapshot(a)
	}
	return out
}

func (c *Collector) SnapshotTool(name string) (ToolSnapshot, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	t, ok := c.tools[name]
	if !ok {
		return ToolSnapshot{}, false
	}
	return ToolSnapshot{Calls: t.calls, AvgResponseMs: avgMs(t.totalResponseMs, t.responseCount)}, true
}

func (c *Collector) SnapshotTools() map[string]ToolSnapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]ToolSnapshot, len(c.tools))
	for name, t := range c.tools {
		out[name] = ToolSnapshot{Calls: t.calls, AvgResponseMs: avgMs(t.totalResponseMs, t.responseCount)}
	}
	return out
}
