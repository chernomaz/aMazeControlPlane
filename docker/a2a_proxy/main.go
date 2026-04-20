// a2a_proxy — Sprint 8 Phase 8B Slice 5.
//
// Terminates the HTTPS/CONNECT blindspot for *cross-organisation* A2A
// traffic, symmetric to what LiteLLM does for LLM calls in Slice 3.
//
// Data flow:
//
//	agent (patched SDK rewrites https://→http://)
//	  → Envoy (sees plaintext JSON-RPC; ext_proc enforces A2A policy)
//	  → a2a_proxy (this service)                    [plain HTTP in]
//	    → TLS to https://partner-agent.example.com  [TLS out]
//
// The request's `Host` header names the real cross-org partner (Envoy
// preserves it — the virtual host matches on the FQDN with no rewrite),
// so the proxy's only job is:
//
//  1. Validate Host against ALLOWED_PARTNERS (defence-in-depth; ext_proc
//     already enforced the allowlist, but failing closed here keeps the
//     sidecar safe if it's ever exposed without a proxy in front).
//  2. Rewrite scheme to https, replay the body verbatim, return the
//     upstream response verbatim.
//
// The self-signed certs on the demo partner-agent are trusted via an
// InsecureSkipVerify toggle (A2A_PROXY_INSECURE_TLS=true) — production
// deployments would mount the partner's CA bundle instead.
package main

import (
	"bytes"
	"crypto/tls"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	addr := envOr("A2A_PROXY_ADDR", ":8082")
	allowed := parseAllowed(os.Getenv("ALLOWED_PARTNERS"))
	insecure := strings.EqualFold(os.Getenv("A2A_PROXY_INSECURE_TLS"), "true")

	client := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{
			// Self-signed partner in demo: skip verification. Set to false
			// in prod and mount the partner CAs into the container.
			TLSClientConfig: &tls.Config{InsecureSkipVerify: insecure},
		},
	}

	h := &handler{allowed: allowed, client: client}

	log.Printf("[a2a_proxy] listening on %s, allowed_partners=%v insecure_tls=%v",
		addr, allowed, insecure)
	if err := http.ListenAndServe(addr, h); err != nil {
		log.Fatalf("[a2a_proxy] listen: %v", err)
	}
}

type handler struct {
	allowed map[string]bool
	client  *http.Client
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Health check — no target semantics.
	if r.Method == http.MethodGet && r.URL.Path == "/healthz" {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
		return
	}

	target := hostWithoutPort(r.Host)
	if target == "" {
		http.Error(w, "missing Host header", http.StatusBadRequest)
		return
	}
	if !h.allowed[target] {
		// Note: we log with target quoted because attacker-controlled Host
		// values could otherwise corrupt the log line (newlines, ANSI).
		log.Printf("[a2a_proxy] deny partner=%q (not in allowlist)", target)
		http.Error(w, "partner not in allowlist", http.StatusForbidden)
		return
	}

	// Read body once — we need to replay it upstream and can't stream
	// without knowing Content-Length is honest.
	//
	// io.LimitReader returns io.EOF at the cap, and io.ReadAll treats EOF
	// as success, so naively wrapping r.Body silently truncates oversized
	// requests. Read one byte past the cap to distinguish "exactly at cap"
	// from "exceeded cap" and fail 413 in the latter case — truncation on
	// a security-sensitive hop must never be silent.
	const maxBodyBytes = 1 << 20
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		http.Error(w, "read body: "+err.Error(), http.StatusBadRequest)
		return
	}
	_ = r.Body.Close()
	if len(body) > maxBodyBytes {
		log.Printf("[a2a_proxy] body exceeds %d bytes partner=%q (got > cap)", maxBodyBytes, target)
		http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		return
	}

	upstreamURL := "https://" + target + r.URL.RequestURI()
	upReq, err := http.NewRequestWithContext(r.Context(), r.Method, upstreamURL, bytes.NewReader(body))
	if err != nil {
		http.Error(w, "build upstream req: "+err.Error(), http.StatusInternalServerError)
		return
	}
	// Copy all client headers except Host (net/http sets that from URL)
	// and hop-by-hop headers that don't make sense across the boundary.
	for k, vs := range r.Header {
		if isHopByHop(k) {
			continue
		}
		for _, v := range vs {
			upReq.Header.Add(k, v)
		}
	}

	resp, err := h.client.Do(upReq)
	if err != nil {
		log.Printf("[a2a_proxy] upstream error partner=%q: %v", target, err)
		http.Error(w, "upstream: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for k, vs := range resp.Header {
		if isHopByHop(k) {
			continue
		}
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(w, resp.Body); err != nil {
		log.Printf("[a2a_proxy] copy response partner=%q: %v", target, err)
	}
}

func parseAllowed(csv string) map[string]bool {
	out := map[string]bool{}
	for _, s := range strings.Split(csv, ",") {
		s = strings.TrimSpace(s)
		if s != "" {
			out[s] = true
		}
	}
	return out
}

func hostWithoutPort(h string) string {
	if i := strings.LastIndex(h, ":"); i >= 0 {
		// Be careful with IPv6 literals — our Hosts are DNS names in
		// practice, but keep the check correct. Host with ":" in the
		// name is a bug upstream, drop to empty rather than guessing.
		if strings.Count(h, ":") == 1 {
			return h[:i]
		}
	}
	return h
}

// hop-by-hop headers per RFC 7230 §6.1 plus a few noisy additions that
// don't belong on cross-org hops.
var hopByHop = map[string]bool{
	"connection":          true,
	"proxy-connection":    true,
	"keep-alive":          true,
	"transfer-encoding":   true,
	"te":                  true,
	"trailer":             true,
	"upgrade":             true,
	"proxy-authorization": true,
	"proxy-authenticate":  true,
	"host":                true, // Go's client sets this from the URL.
}

func isHopByHop(k string) bool {
	return hopByHop[strings.ToLower(k)]
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
