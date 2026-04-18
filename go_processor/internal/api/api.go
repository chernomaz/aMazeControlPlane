package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"

	"amaze/go_processor/internal/stats"
)

type Server struct {
	col *stats.Collector
}

// StartHTTP starts the stats HTTP server on addr in a background goroutine.
func StartHTTP(addr string, col *stats.Collector) {
	s := &Server{col: col}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /stats/agents", s.handleAgents)
	mux.HandleFunc("GET /stats/agents/{id}", s.handleAgent)
	mux.HandleFunc("GET /stats/tools", s.handleTools)
	mux.HandleFunc("GET /stats/tools/{name}", s.handleTool)
	go func() {
		if err := http.ListenAndServe(addr, mux); err != nil {
			fmt.Fprintf(os.Stderr, "[stats-api] failed on %s: %v\n", addr, err)
		}
	}()
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v) //nolint:errcheck
}

func (s *Server) handleAgents(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, s.col.SnapshotAgents())
}

func (s *Server) handleAgent(w http.ResponseWriter, r *http.Request) {
	snap, ok := s.col.SnapshotAgent(r.PathValue("id"))
	if !ok {
		http.NotFound(w, r)
		return
	}
	writeJSON(w, snap)
}

func (s *Server) handleTools(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, s.col.SnapshotTools())
}

func (s *Server) handleTool(w http.ResponseWriter, r *http.Request) {
	snap, ok := s.col.SnapshotTool(r.PathValue("name"))
	if !ok {
		http.NotFound(w, r)
		return
	}
	writeJSON(w, snap)
}
