package enforcer

import (
	"amaze/go_processor/internal/limits"
	"amaze/go_processor/internal/store"
)

func DecideMCP(callerID, serverID, toolName string, bodySize int) (bool, string) {
	policy, ok := store.Get().GetAgent(callerID)
	if !ok {
		return false, "unknown-caller"
	}

	serverAllowed := false
	for _, s := range policy.AllowedMCPServers {
		if s == serverID {
			serverAllowed = true
			break
		}
	}
	if !serverAllowed {
		return false, "mcp-server-not-allowed"
	}

	toolAllowed := false
	for _, t := range policy.AllowedTools[serverID] {
		if t == toolName {
			toolAllowed = true
			break
		}
	}
	if !toolAllowed {
		return false, "tool-not-allowed"
	}

	if l := policy.Limits; l != nil {
		if l.MaxRequestSizeBytes != nil {
			if ok, reason := limits.CheckRequestSize(bodySize, *l.MaxRequestSizeBytes); !ok {
				return false, reason
			}
		}
		if l.MaxRequestsPerMinute != nil {
			window := 60.0
			if l.RateWindowSeconds != nil {
				window = *l.RateWindowSeconds
			}
			if ok, reason := limits.GetRateLimiter().Check(callerID, *l.MaxRequestsPerMinute, window); !ok {
				return false, reason
			}
		}
		if l.PerToolCalls != nil {
			if maxCalls, exists := l.PerToolCalls[toolName]; exists {
				if ok, reason := limits.GetCallCounter().CheckAndIncrement(callerID, serverID, toolName, maxCalls); !ok {
					return false, reason
				}
			}
		}
	}

	return true, "ok"
}
