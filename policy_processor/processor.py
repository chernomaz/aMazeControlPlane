"""
processor.py — ext_proc ProcessingRequest handler.

Sprint 1: passthrough with PASS log.
Sprint 2: A2A allow/deny enforcement using policy_processor.a2a_enforcer.
Sprint 3: Method dispatcher.
  tasks/*   → a2a_enforcer
  tools/call → mcp_enforcer
  MCP protocol messages (initialize, notifications/*, ping, tools/list) → passthrough
  other      → deny (unknown-method)

Each inbound request produces two ext_proc messages:
  1. request_headers  — HTTP headers (x-agent-id, Host / :authority)
  2. request_body     — JSON-RPC payload

Decision happens in the body phase.
"""
import json
import sys
import os

_PROTO_PATH = os.path.join(os.path.dirname(__file__), "proto")
if _PROTO_PATH not in sys.path:
    sys.path.insert(0, _PROTO_PATH)

from envoy.service.ext_proc.v3 import (
    external_processor_pb2 as pb,
    external_processor_pb2_grpc as pb_grpc,
)
from envoy.type.v3 import http_status_pb2

from policy_processor import a2a_enforcer, mcp_enforcer

# MCP protocol messages that are always allowed through without enforcement.
_MCP_PASSTHROUGH = {
    "initialize",
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
    "ping",
    "tools/list",
    "resources/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
}


def _headers_dict(header_map) -> dict[str, str]:
    return {
        h.key.lower(): (h.raw_value.decode() if h.raw_value else h.value)
        for h in header_map.headers
    }


def _continue_headers() -> pb.ProcessingResponse:
    return pb.ProcessingResponse(
        request_headers=pb.HeadersResponse(response=pb.CommonResponse())
    )


def _continue_body() -> pb.ProcessingResponse:
    return pb.ProcessingResponse(
        request_body=pb.BodyResponse(response=pb.CommonResponse())
    )


def _deny(status_code: int, reason: str) -> pb.ProcessingResponse:
    body = json.dumps({"error": "denied", "reason": reason}).encode()
    return pb.ProcessingResponse(
        immediate_response=pb.ImmediateResponse(
            status=http_status_pb2.HttpStatus(code=status_code),
            body=body,
            details=reason,
        )
    )


def _extract_method(body: bytes) -> str:
    try:
        return json.loads(body).get("method", "?")
    except Exception:
        return "?"


def _extract_tool_name(body: bytes) -> str:
    try:
        return json.loads(body).get("params", {}).get("name", "?")
    except Exception:
        return "?"


class ExtProcServicer(pb_grpc.ExternalProcessorServicer):
    """
    Handles the bidirectional ext_proc stream.

    State per stream:
      _caller_id : str  — from x-agent-id request header
      _target_id : str  — from Host / :authority request header
    """

    def Process(self, request_iterator, context):
        caller_id: str = ""
        target_id: str = ""

        for req in request_iterator:
            kind = req.WhichOneof("request")

            # ── headers phase ─────────────────────────────────────────────────
            if kind == "request_headers":
                hdrs = _headers_dict(req.request_headers.headers)
                caller_id = hdrs.get("x-agent-id", "unknown")
                raw_target = hdrs.get(":authority") or hdrs.get("host", "unknown")
                target_id = raw_target.split(":")[0]
                yield _continue_headers()

            # ── body phase ────────────────────────────────────────────────────
            elif kind == "request_body":
                body_bytes = req.request_body.body
                method = _extract_method(body_bytes)

                # MCP protocol handshake — always pass through
                if method in _MCP_PASSTHROUGH:
                    print(
                        f"[ext_proc] PASS  caller={caller_id}  target={target_id}"
                        f"  method={method}  (protocol)",
                        flush=True,
                    )
                    yield _continue_body()

                # A2A enforcement
                elif method.startswith("tasks/"):
                    allow, reason = a2a_enforcer.decide(caller_id, target_id, len(body_bytes))
                    if not allow:
                        print(
                            f"[ext_proc] DENY  caller={caller_id}  target={target_id}"
                            f"  method={method}  reason={reason}",
                            flush=True,
                        )
                        yield _deny(403, reason)
                        return
                    print(
                        f"[ext_proc] PASS  caller={caller_id}  target={target_id}"
                        f"  method={method}  bytes={len(body_bytes)}",
                        flush=True,
                    )
                    yield _continue_body()

                # MCP tool enforcement
                elif method == "tools/call":
                    tool_name = _extract_tool_name(body_bytes)
                    allow, reason = mcp_enforcer.decide(caller_id, target_id, tool_name, len(body_bytes))
                    if not allow:
                        print(
                            f"[ext_proc] DENY  caller={caller_id}  target={target_id}"
                            f"  method={method}  tool={tool_name}  reason={reason}",
                            flush=True,
                        )
                        yield _deny(403, reason)
                        return
                    print(
                        f"[ext_proc] PASS  caller={caller_id}  target={target_id}"
                        f"  method={method}  tool={tool_name}  bytes={len(body_bytes)}",
                        flush=True,
                    )
                    yield _continue_body()

                # Unknown method — fail closed
                else:
                    print(
                        f"[ext_proc] DENY  caller={caller_id}  target={target_id}"
                        f"  method={method}  reason=unknown-method",
                        flush=True,
                    )
                    yield _deny(403, "unknown-method")
                    return

            else:
                pass
