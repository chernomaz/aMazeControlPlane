package store

import (
	"fmt"
	"os"
	"sync"

	"gopkg.in/yaml.v3"
)

type Limits struct {
	MaxRequestSizeBytes  *int           `yaml:"max_request_size_bytes"`
	MaxRequestsPerMinute *int           `yaml:"max_requests_per_minute"`
	RateWindowSeconds    *float64       `yaml:"rate_window_seconds"`
	PerToolCalls         map[string]int `yaml:"per_tool_calls"`
	MaxTokensPerMinute   *int           `yaml:"max_tokens_per_minute"`
}

type AgentPolicy struct {
	AllowedRemoteAgents []string            `yaml:"allowed_remote_agents"`
	AllowedMCPServers   []string            `yaml:"allowed_mcp_servers"`
	AllowedTools        map[string][]string `yaml:"allowed_tools"`
	AllowedLLMs         []string            `yaml:"allowed_llms"`
	Limits              *Limits             `yaml:"limits"`
}

type PolicyStore struct {
	mu     sync.RWMutex
	agents map[string]*AgentPolicy
	path   string
}

var instance *PolicyStore

// Init must be called once from main before any requests are served.
// A path of ":empty:" starts the store with no policies; policies are then pushed
// in at runtime via the config API (used when the Orchestrator owns policy state).
func Init(path string) error {
	if path == ":empty:" {
		instance = &PolicyStore{path: "", agents: map[string]*AgentPolicy{}}
		return nil
	}
	if path == "" {
		path = "policy_processor/policies/agents.yaml"
	}
	s := &PolicyStore{path: path}
	if err := s.Reload(); err != nil {
		return err
	}
	instance = s
	return nil
}

func Get() *PolicyStore { return instance }

func (s *PolicyStore) Reload() error {
	data, err := os.ReadFile(s.path)
	if err != nil {
		return err
	}
	var raw struct {
		Agents map[string]*AgentPolicy `yaml:"agents"`
	}
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return err
	}
	if len(raw.Agents) == 0 {
		return fmt.Errorf("reload aborted: parsed 0 agents (file may be empty or truncated)")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.agents = raw.Agents
	return nil
}

func (s *PolicyStore) GetAgent(agentID string) (*AgentPolicy, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p, ok := s.agents[agentID]
	return p, ok
}

// Upsert replaces (or inserts) the policy for a single agent.
// Orchestrator uses this to push per-agent policy updates without touching the YAML file.
func (s *PolicyStore) Upsert(agentID string, policy *AgentPolicy) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.agents == nil {
		s.agents = map[string]*AgentPolicy{}
	}
	s.agents[agentID] = policy
}

// Delete removes a single agent policy.
func (s *PolicyStore) Delete(agentID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.agents, agentID)
}

// AgentIDs returns the list of currently-known agent IDs (snapshot).
func (s *PolicyStore) AgentIDs() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ids := make([]string, 0, len(s.agents))
	for id := range s.agents {
		ids = append(ids, id)
	}
	return ids
}
