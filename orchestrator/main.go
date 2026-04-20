// Orchestrator — aMaze Control Plane Sprint 8.
//
// Responsibilities:
//   - Accept agent registrations from NEMO containers on startup
//   - Track per-agent lifecycle (PENDING -> RUNNING)
//   - Accept policy uploads from an admin (YAML) and relay them to the
//     Policy Processor's config API
//   - Serve a minimal chat UI and relay user messages to the agent's
//     container chat endpoint
//
// State is kept in memory for Phase 8A. Sprint 10 adds SQLite persistence.
package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	statusPending = "PENDING"
	statusRunning = "RUNNING"
)

// idRe enforces a conservative format for agent and MCP server ids so they
// are safe to substitute into URL paths (Policy Processor config API, chat
// relay) without escaping.
var idRe = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$`)

// hostRe restricts registration-supplied hostnames to DNS-safe characters so
// an agent cannot rewrite the chat-relay target to a URL with embedded creds
// or path components. Compose service names and IPv4 literals both fit.
var hostRe = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$`)

type Agent struct {
	ID           string            `json:"agent_id"`
	Host         string            `json:"host"`         // container DNS name (e.g. "agent-a")
	ChatPort     int               `json:"chat_port"`    // container-internal port for user chat
	A2APort      int               `json:"a2a_port"`     // container-internal port for A2A traffic
	Status       string            `json:"status"`       // PENDING | RUNNING
	RegisteredAt time.Time         `json:"registered_at"`
	UpdatedAt    time.Time         `json:"updated_at"`
	Labels       map[string]string `json:"labels,omitempty"`
}

type Registry struct {
	mu     sync.RWMutex
	agents map[string]*Agent
}

func NewRegistry() *Registry {
	return &Registry{agents: map[string]*Agent{}}
}

func (r *Registry) Upsert(a *Agent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	existing, ok := r.agents[a.ID]
	now := time.Now()
	if ok {
		existing.Host = a.Host
		existing.ChatPort = a.ChatPort
		existing.A2APort = a.A2APort
		existing.UpdatedAt = now
		if a.Status != "" {
			existing.Status = a.Status
		}
		if a.Labels != nil {
			existing.Labels = a.Labels
		}
		return
	}
	a.RegisteredAt = now
	a.UpdatedAt = now
	if a.Status == "" {
		a.Status = statusPending
	}
	r.agents[a.ID] = a
}

func (r *Registry) Get(id string) (*Agent, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	a, ok := r.agents[id]
	return a, ok
}

func (r *Registry) SetStatus(id, status string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	a, ok := r.agents[id]
	if !ok {
		return false
	}
	a.Status = status
	a.UpdatedAt = time.Now()
	return true
}

func (r *Registry) List() []*Agent {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*Agent, 0, len(r.agents))
	for _, a := range r.agents {
		out = append(out, a)
	}
	return out
}

// MCPServer — a registered MCP-server NEMO container. Phase 8B Slice 1.
//
// Unlike agents, MCP servers have no PENDING/RUNNING lifecycle: registration
// is a single-shot advertisement at container startup so the operator UI and
// (future) Envoy routing can resolve the target by name.
type MCPServer struct {
	ID           string            `json:"mcp_id"`
	Host         string            `json:"host"`
	Port         int               `json:"port"`
	RegisteredAt time.Time         `json:"registered_at"`
	Labels       map[string]string `json:"labels,omitempty"`
}

type MCPRegistry struct {
	mu      sync.RWMutex
	servers map[string]*MCPServer
}

func NewMCPRegistry() *MCPRegistry {
	return &MCPRegistry{servers: map[string]*MCPServer{}}
}

// Register inserts or replaces the entry for `s.ID`. MCP servers are
// idempotent: a restarted container simply overwrites its prior record.
func (r *MCPRegistry) Register(s *MCPServer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	s.RegisteredAt = time.Now()
	r.servers[s.ID] = s
}

func (r *MCPRegistry) List() []*MCPServer {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*MCPServer, 0, len(r.servers))
	for _, s := range r.servers {
		out = append(out, s)
	}
	return out
}

// tokenStore — Slice 4 — holds the A2A bearer token minted for each agent.
// Orchestrator mints one 32-byte random token on first register, caches it
// in memory, and pushes it to the Policy Processor so ext_proc can resolve
// `Authorization: Bearer <token>` → agent_id before enforcement runs.
//
// On container restart we replay the cached token so the agent's in-memory
// token reference stays valid across restarts without admin intervention.
type tokenStore struct {
	mu     sync.RWMutex
	tokens map[string]string // agent_id -> opaque token
}

func newTokenStore() *tokenStore {
	return &tokenStore{tokens: map[string]string{}}
}

// GetOrCreate returns the cached token for agentID, minting a fresh random
// one on first call. The returned bool is true iff a new token was minted
// (so the caller knows to push it downstream).
func (t *tokenStore) GetOrCreate(agentID string) (token string, minted bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if tok, ok := t.tokens[agentID]; ok {
		return tok, false
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		// crypto/rand can't realistically fail on Linux; fall back to
		// time-based noise if it ever does. Safer than panicking the
		// whole orchestrator on a transient kernel error.
		return fmt.Sprintf("fallback-%d-%s", time.Now().UnixNano(), agentID), true
	}
	tok := hex.EncodeToString(buf)
	t.tokens[agentID] = tok
	return tok, true
}

func (t *tokenStore) Get(agentID string) (string, bool) {
	t.mu.RLock()
	defer t.mu.RUnlock()
	tok, ok := t.tokens[agentID]
	return tok, ok
}

// policyStore caches YAML policy documents pushed by the admin, keyed by agent_id.
// On container restart, the orchestrator re-plays the cached policy to the Policy
// Processor so the agent goes straight to RUNNING without admin intervention.
type policyStore struct {
	mu    sync.RWMutex
	cache map[string][]byte // agent_id -> YAML bytes
}

func newPolicyStore() *policyStore {
	return &policyStore{cache: map[string][]byte{}}
}

func (p *policyStore) Put(id string, yamlBytes []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	dup := make([]byte, len(yamlBytes))
	copy(dup, yamlBytes)
	p.cache[id] = dup
}

func (p *policyStore) Get(id string) ([]byte, bool) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	y, ok := p.cache[id]
	return y, ok
}

type Server struct {
	reg             *Registry
	mcp             *MCPRegistry
	pol             *policyStore
	tok             *tokenStore
	policyProcessor string // e.g. http://policy-processor:8082
	staticDir       string
}

func main() {
	addr := envOr("ORCH_ADDR", ":7000")
	ppURL := envOr("POLICY_PROCESSOR_CONFIG_URL", "http://localhost:8082")
	staticDir := envOr("ORCH_STATIC_DIR", "./static")

	s := &Server{
		reg:             NewRegistry(),
		mcp:             NewMCPRegistry(),
		pol:             newPolicyStore(),
		tok:             newTokenStore(),
		policyProcessor: ppURL,
		staticDir:       staticDir,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /agents/register", s.handleRegister)
	mux.HandleFunc("GET /agents", s.handleListAgents)
	mux.HandleFunc("GET /agents/{id}", s.handleGetAgent)
	mux.HandleFunc("GET /agents/{id}/status", s.handleStatus)
	mux.HandleFunc("PUT /agents/{id}/policy", s.handlePutPolicy)
	mux.HandleFunc("POST /agents/{id}/chat", s.handleChat)
	mux.HandleFunc("POST /mcp/register", s.handleMCPRegister)
	mux.HandleFunc("GET /mcp", s.handleMCPList)
	mux.HandleFunc("GET /", s.handleStatic)

	log.Printf("[orchestrator] listening on %s, policy processor at %s", addr, ppURL)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("[orchestrator] http: %v", err)
	}
}

// ── registration ─────────────────────────────────────────────────────────────

type registerRequest struct {
	AgentID  string            `json:"agent_id"`
	Host     string            `json:"host"`
	ChatPort int               `json:"chat_port"`
	A2APort  int               `json:"a2a_port"`
	Labels   map[string]string `json:"labels,omitempty"`
}

type registerResponse struct {
	AgentID  string `json:"agent_id"`
	Status   string `json:"status"`
	A2AToken string `json:"a2a_token"`
}

func (s *Server) handleRegister(w http.ResponseWriter, r *http.Request) {
	var req registerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if !idRe.MatchString(req.AgentID) {
		http.Error(w, "invalid agent_id", http.StatusBadRequest)
		return
	}
	if !hostRe.MatchString(req.Host) {
		http.Error(w, "invalid host", http.StatusBadRequest)
		return
	}
	if req.ChatPort <= 0 || req.ChatPort > 65535 || req.A2APort <= 0 || req.A2APort > 65535 {
		http.Error(w, "invalid port", http.StatusBadRequest)
		return
	}

	agent := &Agent{
		ID:       req.AgentID,
		Host:     req.Host,
		ChatPort: req.ChatPort,
		A2APort:  req.A2APort,
		Status:   statusPending,
		Labels:   req.Labels,
	}

	// Slice 4 — mint (or replay) the A2A bearer token before anything
	// else so the Policy Processor learns about it before the agent makes
	// its first outbound A2A call. The token is returned to the container
	// in the register response so the agent can inject it as
	// `Authorization: Bearer <token>` on outbound A2A requests.
	token, _ := s.tok.GetOrCreate(req.AgentID)
	if err := s.pushTokenToProcessor(req.AgentID, token); err != nil {
		// A failed token push leaves the processor without the mapping,
		// which makes every A2A request from this agent resolve to
		// bearerInvalid. Flag it hard — the container can retry register
		// or the operator can inspect logs.
		log.Printf("[orchestrator] token push for %s failed: %v", req.AgentID, err)
	}

	// Fast path: if the admin already pushed a policy for this agent (container
	// restart case), replay it immediately and return RUNNING so the container
	// skips the wait loop.
	if cached, ok := s.pol.Get(req.AgentID); ok {
		if err := s.pushPolicyToProcessor(req.AgentID, cached); err != nil {
			log.Printf("[orchestrator] policy replay for %s failed: %v", req.AgentID, err)
			agent.Status = statusPending
		} else {
			agent.Status = statusRunning
		}
	}
	s.reg.Upsert(agent)

	// Re-read (Upsert may have merged onto an existing record)
	a, _ := s.reg.Get(req.AgentID)
	writeJSON(w, registerResponse{AgentID: a.ID, Status: a.Status, A2AToken: token})
}

// ── listing / status ─────────────────────────────────────────────────────────

func (s *Server) handleListAgents(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]any{"agents": s.reg.List()})
}

func (s *Server) handleGetAgent(w http.ResponseWriter, r *http.Request) {
	a, ok := s.reg.Get(r.PathValue("id"))
	if !ok {
		http.NotFound(w, r)
		return
	}
	writeJSON(w, a)
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	a, ok := s.reg.Get(r.PathValue("id"))
	if !ok {
		http.NotFound(w, r)
		return
	}
	writeJSON(w, map[string]string{"agent_id": a.ID, "status": a.Status})
}

// ── MCP registration ─────────────────────────────────────────────────────────

type mcpRegisterRequest struct {
	MCPID  string            `json:"mcp_id"`
	Host   string            `json:"host"`
	Port   int               `json:"port"`
	Labels map[string]string `json:"labels,omitempty"`
}

type mcpRegisterResponse struct {
	MCPID string `json:"mcp_id"`
}

func (s *Server) handleMCPRegister(w http.ResponseWriter, r *http.Request) {
	var req mcpRegisterRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if !idRe.MatchString(req.MCPID) {
		http.Error(w, "invalid mcp_id", http.StatusBadRequest)
		return
	}
	if !hostRe.MatchString(req.Host) {
		http.Error(w, "invalid host", http.StatusBadRequest)
		return
	}
	if req.Port <= 0 || req.Port > 65535 {
		http.Error(w, "invalid port", http.StatusBadRequest)
		return
	}
	s.mcp.Register(&MCPServer{
		ID:     req.MCPID,
		Host:   req.Host,
		Port:   req.Port,
		Labels: req.Labels,
	})
	writeJSON(w, mcpRegisterResponse{MCPID: req.MCPID})
}

func (s *Server) handleMCPList(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]any{"mcp_servers": s.mcp.List()})
}

// ── policy push ──────────────────────────────────────────────────────────────

// handlePutPolicy accepts a YAML body (the agent's AgentPolicy document),
// caches it locally, relays it to the Policy Processor, then transitions
// the agent to RUNNING.
//
// TODO(sprint-10-auth): this endpoint (and every /agents/* mutation) is
// unauthenticated; exposure is safe only because the orchestrator binds inside
// the compose network. Add admin auth before exposing beyond localhost.
func (s *Server) handlePutPolicy(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !idRe.MatchString(id) {
		http.Error(w, "invalid agent_id", http.StatusBadRequest)
		return
	}
	if _, ok := s.reg.Get(id); !ok {
		http.Error(w, "unknown agent", http.StatusNotFound)
		return
	}
	defer r.Body.Close()
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		http.Error(w, "read body: "+err.Error(), http.StatusBadRequest)
		return
	}
	if len(body) == 0 {
		http.Error(w, "empty body", http.StatusBadRequest)
		return
	}

	if err := s.pushPolicyToProcessor(id, body); err != nil {
		http.Error(w, "policy processor rejected: "+err.Error(), http.StatusBadGateway)
		return
	}
	s.pol.Put(id, body)
	s.reg.SetStatus(id, statusRunning)

	writeJSON(w, map[string]string{"agent_id": id, "status": statusRunning})
}

func (s *Server) pushTokenToProcessor(agentID, token string) error {
	url := fmt.Sprintf("%s/config/tokens/%s", s.policyProcessor, agentID)
	body, _ := json.Marshal(map[string]string{"token": token})
	req, err := http.NewRequest(http.MethodPut, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("status %d: %s", resp.StatusCode, string(bodyBytes))
	}
	return nil
}

func (s *Server) pushPolicyToProcessor(agentID string, yamlBody []byte) error {
	url := fmt.Sprintf("%s/config/agents/%s", s.policyProcessor, agentID)
	req, err := http.NewRequest(http.MethodPut, url, bytes.NewReader(yamlBody))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/yaml")
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("status %d: %s", resp.StatusCode, string(bodyBytes))
	}
	return nil
}

// ── chat relay ───────────────────────────────────────────────────────────────

type chatRequest struct {
	Message string `json:"message"`
}

type chatResponse struct {
	AgentID string `json:"agent_id"`
	Reply   string `json:"reply"`
}

func (s *Server) handleChat(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	a, ok := s.reg.Get(id)
	if !ok {
		http.NotFound(w, r)
		return
	}
	var req chatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid json: "+err.Error(), http.StatusBadRequest)
		return
	}

	url := fmt.Sprintf("http://%s:%d/chat", a.Host, a.ChatPort)
	body, _ := json.Marshal(req)
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		http.Error(w, "chat relay: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	respBytes, _ := io.ReadAll(resp.Body)
	// Propagate the agent's status code so 503 (agent not ready) surfaces to the caller.
	if resp.StatusCode != http.StatusOK {
		w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
		w.WriteHeader(resp.StatusCode)
		w.Write(respBytes)
		return
	}

	var agentReply struct {
		Reply string `json:"reply"`
	}
	_ = json.Unmarshal(respBytes, &agentReply)
	writeJSON(w, chatResponse{AgentID: id, Reply: agentReply.Reply})
}

// ── static files (chat GUI) ──────────────────────────────────────────────────

func (s *Server) handleStatic(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	if path == "/" {
		path = "/index.html"
	}
	clean := filepath.Clean(path)
	full := filepath.Join(s.staticDir, clean)
	if !underDir(full, s.staticDir) {
		http.NotFound(w, r)
		return
	}
	http.ServeFile(w, r, full)
}

func underDir(path, dir string) bool {
	absDir, err := filepath.Abs(dir)
	if err != nil {
		return false
	}
	absPath, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(absDir, absPath)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// ── misc ─────────────────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
