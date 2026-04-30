"""OpenAI-compatible HTTP mock for LLM proxy forwarding tests.

Deployed in compose.test.yml on the amaze-egress network with DNS alias
api.openai.com so the proxy's upstream LLM connections land here instead of
the real API.  Always returns a fixed chat completion with usage data so
token counters and audit fields can be verified without real credentials.
"""
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

_RESPONSE = json.dumps({
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Test response."},
        "finish_reason": "stop",
    }],
    "usage": {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
    },
}).encode()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_RESPONSE)))
        self.end_headers()
        self.wfile.write(_RESPONSE)

    def log_message(self, fmt: str, *args: object) -> None:
        logging.debug(fmt, *args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    srv = HTTPServer(("0.0.0.0", 8000), _Handler)
    logging.info("mock-llm (api.openai.com alias) listening on :8000")
    srv.serve_forever()
