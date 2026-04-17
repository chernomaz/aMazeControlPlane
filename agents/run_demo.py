"""
run_demo.py — Start Agent A and Agent B, watch the A2A hello exchange.

Usage:
    /home/ubuntu/venv/bin/python agents/run_demo.py

What happens:
    1. Agent B (CrewAI,   port 9002) starts first.
    2. Agent A (LangChain, port 9001) starts 1 s later.
    3. Agent A waits 2 s then sends  "hello" → Agent B via A2A.
    4. Agent B prints   "[agent-b/CrewAI]   received hello from agent-a"
    5. Agent B sends    "hello" → Agent A via A2A.
    6. Agent A prints   "[agent-a/LangChain] received hello from agent-b"
"""
import subprocess
import sys
import time
import signal
import os

VENV_PYTHON = "/home/ubuntu/venv/bin/python"
BASE = os.path.dirname(os.path.abspath(__file__))
PORTS = [9001, 9002]

procs: list[subprocess.Popen] = []


def free_ports():
    """Kill any processes still bound to the agent ports."""
    for port in PORTS:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True, text=True
        )
        for pid in result.stdout.strip().splitlines():
            try:
                os.kill(int(pid), signal.SIGKILL)
                print(f"[demo] killed stale process {pid} on port {port}", flush=True)
            except ProcessLookupError:
                pass


def start(module_path: str) -> subprocess.Popen:
    p = subprocess.Popen(
        [VENV_PYTHON, module_path],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    procs.append(p)
    return p


def shutdown(*_):
    print("\n[demo] shutting down …", flush=True)
    for p in procs:
        p.terminate()
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

free_ports()

print("[demo] starting Agent B (CrewAI,    port 9002) …", flush=True)
start(os.path.join(BASE, "agent_b", "main.py"))
time.sleep(1)

print("[demo] starting Agent A (LangChain, port 9001) …", flush=True)
start(os.path.join(BASE, "agent_a", "main.py"))

print("[demo] agents running — waiting for hello exchange …\n", flush=True)

# Keep alive until Ctrl-C
try:
    while True:
        time.sleep(1)
        # Exit if both processes died unexpectedly
        if all(p.poll() is not None for p in procs):
            print("[demo] all agents exited.", flush=True)
            break
except KeyboardInterrupt:
    shutdown()
