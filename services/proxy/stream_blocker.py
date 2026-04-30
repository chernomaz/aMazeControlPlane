"""
StreamBlocker addon — injects "stream": false into LLM request bodies.

Runs after PolicyEnforcer and GraphEnforcer. Only acts on LLM requests
(amaze_kind == "llm"). Ensures complete response bodies so the AuditLog
and Counters addons can parse token usage and record full output.
"""
import json
import logging

from mitmproxy import http

logger = logging.getLogger(__name__)


class StreamBlocker:
    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return
        if flow.metadata.get("amaze_kind") != "llm":
            return
        raw = flow.request.content or b""
        if not raw:
            return
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("stream_blocker: could not parse request body as JSON")
            return
        body["stream"] = False
        flow.request.content = json.dumps(body).encode()
        flow.request.headers["content-length"] = str(len(flow.request.content))
