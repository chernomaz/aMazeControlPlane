"""
server.py — gRPC server for the aMaze Policy Processor.

Listens on port 50051 and serves the ExternalProcessor service
that Envoy calls via ext_proc.

Usage:
    /home/ubuntu/venv/bin/python policy_processor/server.py
"""
import sys
import os
import signal
from concurrent import futures

import grpc

_PROTO_PATH = os.path.join(os.path.dirname(__file__), "proto")
if _PROTO_PATH not in sys.path:
    sys.path.insert(0, _PROTO_PATH)

from envoy.service.ext_proc.v3 import external_processor_pb2_grpc as pb_grpc
from policy_processor.processor import ExtProcServicer

PORT = int(os.environ.get("POLICY_PROCESSOR_PORT", 50051))


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),  # enough for concurrent streams (Sprint 4 adds per-worker locks)
        options=[
            ("grpc.max_receive_message_length", 10 * 1024 * 1024),
        ],
    )
    pb_grpc.add_ExternalProcessorServicer_to_server(ExtProcServicer(), server)
    server.add_insecure_port(f"[::]:{PORT}")
    server.start()
    print(f"[policy-processor] listening on :{PORT}", flush=True)

    def _stop(sig, _):
        print("[policy-processor] shutting down", flush=True)
        server.stop(0)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
