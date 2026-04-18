package limits

import (
	"sync"
	"time"
)

// ── Size ─────────────────────────────────────────────────────────────────────

func CheckRequestSize(bodySize, maxBytes int) (bool, string) {
	if bodySize > maxBytes {
		return false, "request-too-large"
	}
	return true, "ok"
}

// ── Rate limiter ──────────────────────────────────────────────────────────────

type RateLimiter struct {
	mu      sync.Mutex
	windows map[string][]time.Time
}

var globalRateLimiter = &RateLimiter{windows: make(map[string][]time.Time)}

func GetRateLimiter() *RateLimiter { return globalRateLimiter }

func (r *RateLimiter) Check(agentID string, maxRequests int, windowSeconds float64) (bool, string) {
	if windowSeconds <= 0 {
		windowSeconds = 60
	}
	now := time.Now()
	cutoff := now.Add(-time.Duration(float64(time.Second) * windowSeconds))

	r.mu.Lock()
	defer r.mu.Unlock()

	w := r.windows[agentID]
	start := 0
	for start < len(w) && w[start].Before(cutoff) {
		start++
	}
	// Copy to a fresh slice to release the backing array of evicted entries.
	if start > 0 {
		trimmed := make([]time.Time, len(w)-start)
		copy(trimmed, w[start:])
		w = trimmed
	}

	if len(w) >= maxRequests {
		r.windows[agentID] = w
		return false, "rate-limit-exceeded"
	}
	r.windows[agentID] = append(w, now)
	return true, "ok"
}

// ── Call counter ──────────────────────────────────────────────────────────────

type callKey struct{ agent, server, tool string }

type CallCounter struct {
	mu     sync.Mutex
	counts map[callKey]int
}

var globalCallCounter = &CallCounter{counts: make(map[callKey]int)}

func GetCallCounter() *CallCounter { return globalCallCounter }

func (c *CallCounter) CheckAndIncrement(agentID, serverID, toolName string, maxCalls int) (bool, string) {
	k := callKey{agentID, serverID, toolName}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.counts[k] >= maxCalls {
		return false, "call-limit-exceeded"
	}
	c.counts[k]++
	return true, "ok"
}
