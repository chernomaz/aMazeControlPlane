"""Generic HTTP mock server used for MCP tool calls and A2A upstream calls.

Responds 200 to every POST/GET with a minimal JSON-RPC result body.
Deployed twice in compose.test.yml:
  - mock-mcp  (amaze-mcp-net)  — target for MCP proxy forwarding
  - mock-agent (amaze-agent-net, alias test-a2a-callee) — target for A2A forwarding
"""
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def _respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "mock ok"}]},
        }).encode()
        self._respond(body)

    def do_GET(self) -> None:
        self._respond(b'{"status":"ok"}')

    def log_message(self, fmt: str, *args: object) -> None:
        logging.debug(fmt, *args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    srv = HTTPServer(("0.0.0.0", 8000), _Handler)
    logging.info("mock-mcp/mock-agent listening on :8000")
    srv.serve_forever()
