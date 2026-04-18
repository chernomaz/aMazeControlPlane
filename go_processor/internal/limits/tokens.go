package limits

import (
	"sync"
	"time"
)

type tokenEntry struct {
	ts     time.Time
	tokens int
}

type TokenTracker struct {
	mu      sync.Mutex
	windows map[string][]tokenEntry
}

var globalTokenTracker = &TokenTracker{windows: make(map[string][]tokenEntry)}

func GetTokenTracker() *TokenTracker { return globalTokenTracker }

func (t *TokenTracker) Record(agentID string, tokens int) {
	if tokens <= 0 {
		return
	}
	// Prune with a 1-hour ceiling so entries are released even for agents
	// that have no MaxTokensPerMinute (and therefore Check is never called).
	cutoff := time.Now().Add(-time.Hour)
	t.mu.Lock()
	defer t.mu.Unlock()
	w := t.windows[agentID]
	start := 0
	for start < len(w) && w[start].ts.Before(cutoff) {
		start++
	}
	if start > 0 {
		trimmed := make([]tokenEntry, len(w)-start)
		copy(trimmed, w[start:])
		w = trimmed
	}
	t.windows[agentID] = append(w, tokenEntry{ts: time.Now(), tokens: tokens})
}

func (t *TokenTracker) Check(agentID string, maxTokens int, windowSeconds float64) (bool, string) {
	if windowSeconds <= 0 {
		windowSeconds = 60
	}
	cutoff := time.Now().Add(-time.Duration(float64(time.Second) * windowSeconds))

	t.mu.Lock()
	defer t.mu.Unlock()

	w := t.windows[agentID]
	start := 0
	for start < len(w) && w[start].ts.Before(cutoff) {
		start++
	}
	if start > 0 {
		trimmed := make([]tokenEntry, len(w)-start)
		copy(trimmed, w[start:])
		w = trimmed
		t.windows[agentID] = w
	}

	total := 0
	for _, e := range w {
		total += e.tokens
	}
	if total >= maxTokens {
		return false, "token-limit-exceeded"
	}
	return true, "ok"
}
