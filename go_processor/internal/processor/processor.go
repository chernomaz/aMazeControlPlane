package processor

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"time"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	envoytypev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"amaze/go_processor/internal/enforcer"
	"amaze/go_processor/internal/limits"
	"amaze/go_processor/internal/stats"
	"amaze/go_processor/internal/tokens"
)

// bearerState distinguishes "no bearer was attempted" from "bearer present
// but unknown" so the A2A branch can require a valid bearer while the MCP
// and LLM branches keep falling back to x-agent-id.
type bearerState int

const (
	bearerAbsent bearerState = iota
	bearerResolved
	bearerInvalid
)

// llmTargets — which Host/:authority values should be dispatched through the
// LLM enforcer. "litellm" is the Sprint 8 Slice 3 sidecar that fronts real
// OpenAI/Anthropic/etc. endpoints; "openai-api" and "anthropic-api" are the
// Sprint 6 direct-route virtual hosts (still used by non-containerised demos).
var llmTargets = map[string]bool{
	"openai-api":    true,
	"anthropic-api": true,
	"litellm":       true,
}

var mcpPassthrough = map[string]bool{
	"initialize":                true,
	"notifications/initialized": true,
	"notifications/cancelled":   true,
	"notifications/progress":    true,
	"ping":                      true,
	"tools/list":                true,
	"resources/list":            true,
	"resources/read":            true,
	"prompts/list":              true,
	"prompts/get":               true,
}

type Server struct {
	extprocv3.UnimplementedExternalProcessorServer
	Stats *stats.Collector
}

func (s *Server) Process(stream extprocv3.ExternalProcessor_ProcessServer) error {
	var callerID, targetID string
	var bs bearerState
	var tStart time.Time   // set when a request is allowed; zero for passthrough/denied
	var allowedTool string // non-empty for allowed MCP tools/call

	for {
		req, err := stream.Recv()
		if err != nil {
			code := status.Code(err)
			if err == io.EOF || code == codes.Canceled || code == codes.Unavailable {
				return nil
			}
			return status.Errorf(codes.Unknown, "recv: %v", err)
		}

		switch r := req.Request.(type) {

		case *extprocv3.ProcessingRequest_RequestHeaders:
			callerID, targetID, bs = extractIDs(r.RequestHeaders)
			// Sprint 9 — two-branch header mutation on the upstream request:
			//
			//   bearerResolved → OVERWRITE `x-amaze-caller` with the
			//       authenticated caller id. The SDK on the receiving end
			//       reads ONLY this header for `receive_message_from_agent`'s
			//       first argument — so a forged `params.from` or a
			//       pre-set `x-amaze-caller` from the sender is neutralised.
			//
			//   bearerAbsent / bearerInvalid → REMOVE any client-supplied
			//       `x-amaze-caller`. We have no authenticated sender to
			//       vouch for, so letting the client's value through would
			//       reintroduce the spoof vector on paths that read it
			//       (A2A today; any future path).
			//
			// Done at RequestHeaders phase (not body phase) because
			// body-phase HeaderMutation was observed silently dropped
			// against our `processing_mode: BUFFERED` config. Header
			// phase is the reliable mutation point for this Envoy build.
			var hResp *extprocv3.ProcessingResponse
			if bs == bearerResolved {
				hResp = continueHeadersWithCaller(callerID)
			} else {
				hResp = continueHeadersStripCaller()
			}
			if err := stream.Send(hResp); err != nil {
				return err
			}

		case *extprocv3.ProcessingRequest_RequestBody:
			body := r.RequestBody.Body
			bodySize := len(body)
			tStart = time.Time{}
			allowedTool = ""

			var resp *extprocv3.ProcessingResponse

			if llmTargets[targetID] {
				allow, reason := enforcer.DecideLLM(callerID, targetID, bodySize)
				if !allow {
					fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  type=llm  reason=%s\n",
						callerID, targetID, reason)
					// Split status codes by reason semantics — 429 is
					// "rate-limited, retry later"; everything else is
					// "forbidden, don't retry". Before Sprint 8 all LLM
					// denies collapsed to 429 which misled clients into
					// retrying fatal errors (e.g. `llm-not-allowed`).
					resp = deny(llmDenyStatus(reason), reason)
					s.Stats.RecordRequest(callerID, "", false)
				} else {
					fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  type=llm  bytes=%d\n",
						callerID, targetID, bodySize)
					resp = continueBody()
					s.Stats.RecordRequest(callerID, "", true)
					tStart = time.Now()
				}
			} else {
				method := extractMethod(body)
				switch {
				case mcpPassthrough[method]:
					fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  method=%s  (protocol)\n",
						callerID, targetID, method)
					resp = continueBody()
					// no stats for protocol handshake

				case strings.HasPrefix(method, "tasks/"):
					// Slice 4 — A2A requires a valid bearer (A2A spec has
					// native auth primitives; x-agent-id is only a fallback
					// for MCP/LLM). bearerInvalid = bearer was sent but
					// isn't in the registry; bearerAbsent = no Authorization
					// header at all. Split reasons so tests + operators can
					// distinguish "bad token" from "no token".
					if bs == bearerInvalid {
						fmt.Printf("[ext_proc] DENY  target=%s  method=%s  reason=invalid-bearer\n",
							targetID, method)
						resp = deny(403, "invalid-bearer")
						s.Stats.RecordRequest("unknown", "", false)
						break
					}
					if bs != bearerResolved {
						fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  reason=missing-bearer\n",
							callerID, targetID, method)
						resp = deny(403, "missing-bearer")
						s.Stats.RecordRequest(callerID, "", false)
						break
					}
					allow, reason := enforcer.DecideA2A(callerID, targetID, bodySize)
					if !allow {
						fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  reason=%s\n",
							callerID, targetID, method, reason)
						resp = deny(403, reason)
						s.Stats.RecordRequest(callerID, "", false)
					} else {
						fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  method=%s  bytes=%d\n",
							callerID, targetID, method, bodySize)
						// x-amaze-caller was already injected in the
						// RequestHeaders phase (see extractIDs dispatch above).
						resp = continueBody()
						s.Stats.RecordRequest(callerID, "", true)
						tStart = time.Now()
					}

				case method == "tools/call":
					toolName := extractToolName(body)
					allow, reason := enforcer.DecideMCP(callerID, targetID, toolName, bodySize)
					if !allow {
						fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  tool=%s  reason=%s\n",
							callerID, targetID, method, toolName, reason)
						resp = deny(403, reason)
						s.Stats.RecordRequest(callerID, "", false)
					} else {
						fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  method=%s  tool=%s  bytes=%d\n",
							callerID, targetID, method, toolName, bodySize)
						resp = continueBody()
						s.Stats.RecordRequest(callerID, toolName, true)
						tStart = time.Now()
						allowedTool = toolName
					}

				default:
					fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  reason=unknown-method\n",
						callerID, targetID, method)
					resp = deny(403, "unknown-method")
					s.Stats.RecordRequest(callerID, "", false)
				}
			}

			if err := stream.Send(resp); err != nil {
				return err
			}
			if _, isDeny := resp.Response.(*extprocv3.ProcessingResponse_ImmediateResponse); isDeny {
				return nil
			}

		case *extprocv3.ProcessingRequest_ResponseBody:
			tokens := extractTokens(r.ResponseBody.Body)
			if tokens > 0 {
				limits.GetTokenTracker().Record(callerID, tokens)
				fmt.Printf("[ext_proc] TOKENS caller=%s  tokens=%d\n", callerID, tokens)
			}
			if !tStart.IsZero() {
				s.Stats.RecordResponse(callerID, allowedTool, time.Since(tStart), tokens)
			}
			if err := stream.Send(continueResponseBody()); err != nil {
				return err
			}
		}
	}
}

// extractIDs reads caller identity + upstream target from the request headers.
//
// `Authorization: Bearer` is re-used by non-A2A SDKs (openai sends the
// API key there, anthropic likewise). We therefore *don't* universally
// hard-fail on unknown bearers — only the A2A branch treats bearerInvalid
// as fatal. For other traffic types the bearer is ignored and x-agent-id
// is the identity.
//
// Priority of the returned callerID:
//
//  1. `Authorization: Bearer <token>` resolvable via tokens.Store → from token, bs=bearerResolved
//  2. `Authorization: Bearer …` present but unresolvable          → falls back to x-agent-id, bs=bearerInvalid (A2A branch denies; MCP/LLM ignore and proceed)
//  3. no Authorization bearer, `x-agent-id` set                   → from header, bs=bearerAbsent
//  4. neither                                                     → "unknown", bs=bearerAbsent
//
// Note (Sprint 9): this function does NOT read `x-amaze-caller`. That
// header is injected by *us* in the headers-response phase and is meant
// as output-only trust: clients never supply it, and the header-mutation
// path overwrites or removes any value a client tried to sneak in.
func extractIDs(h *extprocv3.HttpHeaders) (callerID, targetID string, bs bearerState) {
	callerID = "unknown"
	targetID = "unknown"
	bs = bearerAbsent
	if h == nil || h.Headers == nil {
		return
	}
	var authority, host, xAgentID, bearer string
	for _, hdr := range h.Headers.Headers {
		key := strings.ToLower(hdr.Key)
		val := hdr.Value
		if len(hdr.RawValue) > 0 {
			val = string(hdr.RawValue)
		}
		switch key {
		case "x-agent-id":
			xAgentID = val
		case ":authority":
			authority = val
		case "host":
			host = val
		case "authorization":
			if len(val) > 7 && strings.EqualFold(val[:7], "Bearer ") {
				bearer = strings.TrimSpace(val[7:])
			}
		}
	}
	raw := authority
	if raw == "" {
		raw = host
	}
	if raw == "" {
		raw = "unknown"
	}
	targetID = strings.SplitN(raw, ":", 2)[0]

	if bearer != "" {
		if id, ok := tokens.Get().Resolve(bearer); ok {
			callerID = id
			bs = bearerResolved
			return
		}
		bs = bearerInvalid
		// Intentionally fall through to x-agent-id below — lets non-A2A
		// traffic (openai SDK sending its API key as Bearer) continue
		// working; the A2A branch uses bs==bearerResolved as its gate
		// so an unresolved bearer still can't authenticate A2A.
	}
	if xAgentID != "" {
		callerID = xAgentID
	}
	return
}

func extractMethod(body []byte) string {
	var m struct {
		Method string `json:"method"`
	}
	if err := json.Unmarshal(body, &m); err != nil || m.Method == "" {
		return "?"
	}
	return m.Method
}

func extractToolName(body []byte) string {
	var m struct {
		Params struct {
			Name string `json:"name"`
		} `json:"params"`
	}
	if err := json.Unmarshal(body, &m); err != nil {
		return "?"
	}
	return m.Params.Name
}

func extractTokens(body []byte) int {
	if n := parseTokensJSON(body); n > 0 {
		return n
	}
	// LLM providers compress responses with gzip when the client sends Accept-Encoding.
	// Envoy buffers the raw (compressed) bytes, so we must decompress before parsing.
	gr, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		return 0
	}
	defer gr.Close()
	plain, err := io.ReadAll(gr)
	if err != nil {
		return 0
	}
	return parseTokensJSON(plain)
}

func parseTokensJSON(body []byte) int {
	var resp map[string]any
	if err := json.Unmarshal(body, &resp); err != nil {
		return 0
	}
	usage, ok := resp["usage"].(map[string]any)
	if !ok {
		return 0
	}
	// OpenAI: total_tokens
	if t, ok := usage["total_tokens"].(float64); ok {
		return int(t)
	}
	// Anthropic: input_tokens + output_tokens
	if i, ok := usage["input_tokens"].(float64); ok {
		o, _ := usage["output_tokens"].(float64)
		return int(i + o)
	}
	return 0
}

func continueHeaders() *extprocv3.ProcessingResponse {
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_RequestHeaders{
			RequestHeaders: &extprocv3.HeadersResponse{},
		},
	}
}

// continueHeadersWithCaller injects the authenticated caller id as
// `x-amaze-caller` on the upstream request. `OVERWRITE_IF_EXISTS_OR_ADD`
// replaces any client-supplied value atomically — we tried a separate
// `RemoveHeaders` first and discovered Envoy applies the remove AFTER
// the set, leaving the upstream with an empty `x-amaze-caller: ''` and
// defeating the whole point. Trust the overwrite action alone.
func continueHeadersWithCaller(callerID string) *extprocv3.ProcessingResponse {
	// Envoy's ext_proc silently treats `HeaderValue.Value` as empty on set
	// mutations in some builds — use `RawValue` (bytes) which is the
	// reliable path. Enum wire-value for OVERWRITE_IF_EXISTS_OR_ADD is 2;
	// the constant is named `corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD`.
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_RequestHeaders{
			RequestHeaders: &extprocv3.HeadersResponse{
				Response: &extprocv3.CommonResponse{
					HeaderMutation: &extprocv3.HeaderMutation{
						SetHeaders: []*corev3.HeaderValueOption{
							{
								Header: &corev3.HeaderValue{
									Key:      "x-amaze-caller",
									RawValue: []byte(callerID),
								},
								AppendAction: corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD,
							},
						},
					},
				},
			},
		},
	}
}

// continueHeadersStripCaller removes any client-supplied `x-amaze-caller`
// without installing one of our own. Used when bearer didn't resolve:
// MCP/LLM paths that rely on x-agent-id, and unknown bearers on any path.
// Stripping matters because the SDK treats x-amaze-caller as trust-rooted;
// if ext_proc can't vouch for the value we must not let the client's
// version pass through.
func continueHeadersStripCaller() *extprocv3.ProcessingResponse {
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_RequestHeaders{
			RequestHeaders: &extprocv3.HeadersResponse{
				Response: &extprocv3.CommonResponse{
					HeaderMutation: &extprocv3.HeaderMutation{
						RemoveHeaders: []string{"x-amaze-caller"},
					},
				},
			},
		},
	}
}

func continueBody() *extprocv3.ProcessingResponse {
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_RequestBody{
			RequestBody: &extprocv3.BodyResponse{},
		},
	}
}

func continueResponseBody() *extprocv3.ProcessingResponse {
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_ResponseBody{
			ResponseBody: &extprocv3.BodyResponse{},
		},
	}
}

// llmDenyStatus maps an LLM enforcer deny reason to the right HTTP status.
// Only token-limit-exceeded is retryable (rate-limit semantics); unknown-caller,
// llm-not-allowed, and request-too-large are permanent authorisation failures
// that the caller must not retry.
func llmDenyStatus(reason string) int {
	if reason == "token-limit-exceeded" {
		return 429
	}
	return 403
}

func deny(statusCode int, reason string) *extprocv3.ProcessingResponse {
	body, _ := json.Marshal(map[string]string{"error": "denied", "reason": reason})
	return &extprocv3.ProcessingResponse{
		Response: &extprocv3.ProcessingResponse_ImmediateResponse{
			ImmediateResponse: &extprocv3.ImmediateResponse{
				Status:  &envoytypev3.HttpStatus{Code: envoytypev3.StatusCode(statusCode)},
				Body:    body,
				Details: reason,
			},
		},
	}
}
