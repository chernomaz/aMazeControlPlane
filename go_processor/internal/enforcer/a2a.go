package enforcer

import (
	"amaze/go_processor/internal/limits"
	"amaze/go_processor/internal/store"
)

func DecideA2A(callerID, targetID string, bodySize int) (bool, string) {
	policy, ok := store.Get().GetAgent(callerID)
	if !ok {
		return false, "unknown-caller"
	}

	allowed := false
	for _, a := range policy.AllowedRemoteAgents {
		if a == targetID {
			allowed = true
			break
		}
	}
	if !allowed {
		return false, "not-allowed"
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
	}

	return true, "ok"
}
