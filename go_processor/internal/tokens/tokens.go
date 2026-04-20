// Package tokens holds the in-memory A2A bearer-token registry.
//
// The Orchestrator mints one token per agent on first registration and
// PUTs it here via the config API (/config/tokens/{agent_id}). The
// processor's request-headers hook then resolves `Authorization: Bearer
// <token>` to the caller's agent_id before any policy check — so the
// A2A spec's native auth primitive becomes the trust root for A2A
// traffic, while `x-agent-id` stays as the fallback for MCP/LLM (no
// equivalent auth primitive).
package tokens

import (
	"sync"
)

type Store struct {
	mu sync.RWMutex
	// Forward: agent_id → token. Used when Orchestrator rotates/replays.
	byAgent map[string]string
	// Reverse: token → agent_id. Hot path for every A2A request.
	byToken map[string]string
}

var instance = &Store{
	byAgent: map[string]string{},
	byToken: map[string]string{},
}

func Get() *Store { return instance }

// Put installs the (agent_id, token) pair, overwriting any previous token
// for the same agent. The previous token (if any) is removed from the
// reverse map so stale tokens stop resolving immediately after rotation.
func (s *Store) Put(agentID, token string) {
	if agentID == "" || token == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if prev, ok := s.byAgent[agentID]; ok && prev != token {
		delete(s.byToken, prev)
	}
	s.byAgent[agentID] = token
	s.byToken[token] = agentID
}

// Delete removes the agent's token entirely. Follow-up A2A requests
// carrying the old token will resolve to bearerInvalid.
func (s *Store) Delete(agentID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if tok, ok := s.byAgent[agentID]; ok {
		delete(s.byToken, tok)
		delete(s.byAgent, agentID)
	}
}

// Resolve returns (agent_id, true) if the token is known, ("", false) otherwise.
func (s *Store) Resolve(token string) (string, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	id, ok := s.byToken[token]
	return id, ok
}

// AgentIDs — snapshot, for a future GET /config/tokens admin endpoint.
func (s *Store) AgentIDs() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]string, 0, len(s.byAgent))
	for id := range s.byAgent {
		out = append(out, id)
	}
	return out
}
