package enforcer

import (
	"amaze/go_processor/internal/limits"
	"amaze/go_processor/internal/store"
)

// DecideLLM enforces token budget for requests bound for LLM endpoints.
// Check order: size → token budget.
func DecideLLM(callerID string, bodySize int) (bool, string) {
	policy, ok := store.Get().GetAgent(callerID)
	if !ok {
		return false, "unknown-caller"
	}

	if l := policy.Limits; l != nil {
		if l.MaxRequestSizeBytes != nil {
			if ok, reason := limits.CheckRequestSize(bodySize, *l.MaxRequestSizeBytes); !ok {
				return false, reason
			}
		}
		if l.MaxTokensPerMinute != nil {
			window := 60.0
			if l.RateWindowSeconds != nil {
				window = *l.RateWindowSeconds
			}
			if ok, reason := limits.GetTokenTracker().Check(callerID, *l.MaxTokensPerMinute, window); !ok {
				return false, reason
			}
		}
	}

	return true, "ok"
}
