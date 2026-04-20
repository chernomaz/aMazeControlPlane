package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"amaze/go_processor/internal/store"
	"amaze/go_processor/internal/tokens"
	"gopkg.in/yaml.v3"
)

const maxPolicyBodyBytes = 1 << 20 // 1 MiB

// StartConfigHTTP starts the config HTTP server on addr.
// This endpoint is used by the Orchestrator to push per-agent policies into the running
// Policy Processor, so new agents can start being enforced without editing the YAML file
// or restarting the process.
func StartConfigHTTP(addr string) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /config/agents", handleListAgents)
	mux.HandleFunc("GET /config/agents/{id}", handleGetAgent)
	mux.HandleFunc("PUT /config/agents/{id}", handlePutAgent)
	mux.HandleFunc("DELETE /config/agents/{id}", handleDeleteAgent)
	// Slice 4 — A2A bearer tokens. Orchestrator mints a random token per
	// agent on first register and PUTs it here so ext_proc can resolve
	// Authorization: Bearer <token> → agent_id before enforcement.
	mux.HandleFunc("PUT /config/tokens/{id}", handlePutToken)
	mux.HandleFunc("DELETE /config/tokens/{id}", handleDeleteToken)
	go func() {
		if err := http.ListenAndServe(addr, mux); err != nil {
			fmt.Fprintf(os.Stderr, "[config-api] failed on %s: %v\n", addr, err)
		}
	}()
}

func handleListAgents(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]any{"agents": store.Get().AgentIDs()})
}

func handleGetAgent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	p, ok := store.Get().GetAgent(id)
	if !ok {
		http.NotFound(w, r)
		return
	}
	writeJSON(w, p)
}

// handlePutAgent accepts either a JSON body matching AgentPolicy, or a YAML body
// wrapped in {"yaml": "<yaml text>"}. YAML support lets the Orchestrator relay the
// admin's original policy document verbatim.
func handlePutAgent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		http.Error(w, "missing agent id", http.StatusBadRequest)
		return
	}
	defer r.Body.Close()

	limited := io.LimitReader(r.Body, maxPolicyBodyBytes)
	ctype := strings.ToLower(r.Header.Get("Content-Type"))
	isYAML := strings.HasPrefix(ctype, "application/yaml") || strings.HasPrefix(ctype, "text/yaml")
	var policy store.AgentPolicy

	if isYAML {
		if err := yaml.NewDecoder(limited).Decode(&policy); err != nil {
			http.Error(w, fmt.Sprintf("invalid yaml: %v", err), http.StatusBadRequest)
			return
		}
	} else {
		if err := json.NewDecoder(limited).Decode(&policy); err != nil {
			http.Error(w, fmt.Sprintf("invalid json: %v", err), http.StatusBadRequest)
			return
		}
	}
	store.Get().Upsert(id, &policy)
	writeJSON(w, map[string]any{"status": "ok", "agent_id": id})
}

func handleDeleteAgent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	store.Get().Delete(id)
	writeJSON(w, map[string]any{"status": "ok", "agent_id": id})
}

// handlePutToken accepts JSON {"token": "<opaque-string>"}. The raw token is
// stored verbatim in the reverse-lookup map; callers are responsible for
// picking something high-entropy (Orchestrator uses crypto/rand 32 bytes).
func handlePutToken(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		http.Error(w, "missing agent id", http.StatusBadRequest)
		return
	}
	defer r.Body.Close()
	var body struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&body); err != nil {
		http.Error(w, fmt.Sprintf("invalid json: %v", err), http.StatusBadRequest)
		return
	}
	if body.Token == "" {
		http.Error(w, "missing token", http.StatusBadRequest)
		return
	}
	tokens.Get().Put(id, body.Token)
	writeJSON(w, map[string]any{"status": "ok", "agent_id": id})
}

func handleDeleteToken(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	tokens.Get().Delete(id)
	writeJSON(w, map[string]any{"status": "ok", "agent_id": id})
}
