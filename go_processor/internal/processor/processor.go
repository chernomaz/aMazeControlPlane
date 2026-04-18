package processor

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"time"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	envoytypev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"amaze/go_processor/internal/enforcer"
	"amaze/go_processor/internal/limits"
	"amaze/go_processor/internal/stats"
)

var llmTargets = map[string]bool{
	"openai-api":    true,
	"anthropic-api": true,
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
	var tStart time.Time    // set when a request is allowed; zero for passthrough/denied
	var allowedTool string  // non-empty for allowed MCP tools/call

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
			callerID, targetID = extractIDs(r.RequestHeaders)
			if err := stream.Send(continueHeaders()); err != nil {
				return err
			}

		case *extprocv3.ProcessingRequest_RequestBody:
			body := r.RequestBody.Body
			bodySize := len(body)
			tStart = time.Time{}
			allowedTool = ""

			var resp *extprocv3.ProcessingResponse

			if llmTargets[targetID] {
				allow, reason := enforcer.DecideLLM(callerID, bodySize)
				if !allow {
					fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  type=llm  reason=%s\n",
						callerID, targetID, reason)
					resp = deny(429, reason)
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
					allow, reason := enforcer.DecideA2A(callerID, targetID, bodySize)
					if !allow {
						fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  reason=%s\n",
							callerID, targetID, method, reason)
						resp = deny(403, reason)
						s.Stats.RecordRequest(callerID, "", false)
					} else {
						fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  method=%s  bytes=%d\n",
							callerID, targetID, method, bodySize)
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

func extractIDs(h *extprocv3.HttpHeaders) (callerID, targetID string) {
	callerID = "unknown"
	targetID = "unknown"
	if h == nil || h.Headers == nil {
		return
	}
	var authority, host string
	for _, hdr := range h.Headers.Headers {
		key := strings.ToLower(hdr.Key)
		val := hdr.Value
		if len(hdr.RawValue) > 0 {
			val = string(hdr.RawValue)
		}
		switch key {
		case "x-agent-id":
			callerID = val
		case ":authority":
			authority = val
		case "host":
			host = val
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
