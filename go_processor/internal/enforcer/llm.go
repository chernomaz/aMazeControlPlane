package enforcer

import (
	"amaze/go_processor/internal/limits"
	"amaze/go_processor/internal/store"
)

// DecideLLM enforces access to LLM targets (openai-api, anthropic-api, litellm, …)
// for the given caller. Check order: allowlist → size → token budget.
//
// targetID is the Host / :authority the agent used to reach the LLM gateway;
// Sprint 8 Slice 3 adds the LiteLLM sidecar so a single target ("litellm")
// fronts multiple upstream providers.
func DecideLLM(callerID, targetID string, bodySize int) (bool, string) {
	policy, ok := store.Get().GetAgent(callerID)
	if !ok {
		return false, "unknown-caller"
	}

	if !contains(policy.AllowedLLMs, targetID) {
		return false, "llm-not-allowed"
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

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}
