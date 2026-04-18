package processor

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	envoytypev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"amaze/go_processor/internal/enforcer"
)

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
}

func (s *Server) Process(stream extprocv3.ExternalProcessor_ProcessServer) error {
	var callerID, targetID string

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
			method := extractMethod(body)
			bodySize := len(body)

			var resp *extprocv3.ProcessingResponse

			switch {
			case mcpPassthrough[method]:
				fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  method=%s  (protocol)\n",
					callerID, targetID, method)
				resp = continueBody()

			case strings.HasPrefix(method, "tasks/"):
				allow, reason := enforcer.DecideA2A(callerID, targetID, bodySize)
				if !allow {
					fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  reason=%s\n",
						callerID, targetID, method, reason)
					resp = deny(403, reason)
				} else {
					fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  method=%s  bytes=%d\n",
						callerID, targetID, method, bodySize)
					resp = continueBody()
				}

			case method == "tools/call":
				toolName := extractToolName(body)
				allow, reason := enforcer.DecideMCP(callerID, targetID, toolName, bodySize)
				if !allow {
					fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  tool=%s  reason=%s\n",
						callerID, targetID, method, toolName, reason)
					resp = deny(403, reason)
				} else {
					fmt.Printf("[ext_proc] PASS  caller=%s  target=%s  method=%s  tool=%s  bytes=%d\n",
						callerID, targetID, method, toolName, bodySize)
					resp = continueBody()
				}

			default:
				fmt.Printf("[ext_proc] DENY  caller=%s  target=%s  method=%s  reason=unknown-method\n",
					callerID, targetID, method)
				resp = deny(403, "unknown-method")
			}

			if err := stream.Send(resp); err != nil {
				return err
			}
			if _, isDeny := resp.Response.(*extprocv3.ProcessingResponse_ImmediateResponse); isDeny {
				return nil
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
