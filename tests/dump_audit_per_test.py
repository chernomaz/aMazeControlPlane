"""Run each Sprint 1 integration test scenario and dump the full audit log
record-by-record so the human can inspect what the proxy captured.

Usage:
    /home/ubuntu/venv/bin/python tests/dump_audit_per_test.py
    /home/ubuntu/venv/bin/python tests/dump_audit_per_test.py --out tests/audit_dump_latest.txt
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import time

import httpx

AGENT_SDK = "http://localhost:8090"
PLATFORM = "amaze-platform"
AGENTS = ["agent-sdk", "agent-sdk1", "agent-sdk2"]
HTTP_TIMEOUT = 90.0


# ── ANSI colors ─────────────────────────────────────────────────────────────
C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
}


def cli(*args: str) -> str:
    out = subprocess.run(
        ["docker", "exec", PLATFORM, "redis-cli", *args],
        capture_output=True, text=True, check=False, timeout=10,
    )
    return out.stdout


def cli_json(*args: str):
    out = subprocess.run(
        ["docker", "exec", PLATFORM, "redis-cli", "--json", *args],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def reset_session_counters() -> int:
    """Clear per-session counters AND any cached trace contexts so each
    test scenario starts a fresh conversation with its own trace_id."""
    total = 0
    for pattern in ("session:*", "trace_context:*"):
        keys = cli_json("KEYS", pattern) or []
        if keys:
            cli("DEL", *keys)
            total += len(keys)
    return total


def audit_records(agent_id: str, since_ms: int) -> list[dict]:
    data = cli_json("XRANGE", f"audit:{agent_id}", f"{since_ms}-0", "+") or []
    out = []
    for entry in data:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        eid, fields = entry
        rec = {"id": eid}
        for i in range(0, len(fields), 2):
            rec[fields[i]] = fields[i + 1]
        out.append(rec)
    return out


def fmt_kind(rec: dict) -> str:
    kind = rec.get("kind", "?")
    color = {
        "llm": C["blue"], "mcp": C["cyan"], "a2a": C["magenta"],
        "unknown": C["yellow"],
    }.get(kind, C["dim"])
    suffix = ""
    if kind == "llm":
        if rec.get("indirect") == "true":
            suffix = f"{C['violet'] if 'violet' in C else C['magenta']}*{C['reset']}"  # tool-dispatch
        elif rec.get("has_tool_calls_input") == "true":
            suffix = f"{C['green']}↩{C['reset']}"  # synthesis after tool results
    return f"{color}{kind:<6}{C['reset']}{suffix}"


def fmt_status(rec: dict) -> str:
    if rec.get("denied", "false") == "true":
        return f"{C['red']}DENIED{C['reset']}"
    return f"{C['green']}allow{C['reset']} "


def print_record(idx: int, rec: dict, full: bool = True) -> None:
    target = rec.get("target") or "—"
    tool = rec.get("tool") or "—"
    reason = rec.get("denial_reason") or ""
    inp = rec.get("input", "") or "(empty)"
    out = rec.get("output", "") or "(empty)"
    trace = rec.get("trace_id") or "—"
    span = rec.get("span_id") or "—"
    agent = rec.get("agent_id", "")
    session = rec.get("session_id", "")
    ts = rec.get("ts", "")

    head = (
        f"  {C['bold']}[{idx:>2}]{C['reset']} "
        f"{fmt_kind(rec)} {fmt_status(rec)} "
        f"target={C['dim']}{target}{C['reset']} "
        f"tool={C['dim']}{tool}{C['reset']}"
    )
    print(head)
    print(f"      {C['dim']}stream-id  {C['reset']}: {rec.get('id','')}")
    print(f"      {C['dim']}ts         {C['reset']}: {ts}")
    print(f"      {C['dim']}agent      {C['reset']}: {agent}")
    print(f"      {C['dim']}session    {C['reset']}: {session}")
    print(f"      {C['dim']}trace_id   {C['reset']}: {trace}")
    print(f"      {C['dim']}span_id    {C['reset']}: {span}")
    if reason:
        print(f"      {C['red']}denial     {C['reset']}: {reason}")
    alert = rec.get("alert", "")
    if alert:
        print(f"      {C['yellow']}alert      {C['reset']}: {alert}")
    if rec.get("kind") == "llm":
        ind = rec.get("indirect", "false") == "true"
        synth = rec.get("has_tool_calls_input", "false") == "true"
        if ind or synth:
            tags = []
            if ind: tags.append("indirect (returned tool_calls)")
            if synth: tags.append("synthesis (input had tool results)")
            print(f"      {C['cyan']}llm-flags  {C['reset']}: {', '.join(tags)}")
    print(f"      {C['dim']}input      {C['reset']}:")
    for line in str(inp).splitlines() or [""]:
        print(f"        {line}")
    print(f"      {C['dim']}output     {C['reset']}:")
    for line in str(out).splitlines() or [""]:
        print(f"        {line}")
    print()


def post_chat(message: str) -> tuple[int, dict]:
    with httpx.Client(timeout=HTTP_TIMEOUT) as cli_:
        r = cli_.post(f"{AGENT_SDK}/chat", json={"message": message})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text}


def get_bearer_for(agent_id: str) -> str | None:
    """Look up a session_token in Redis whose value matches the agent_id."""
    keys_json = cli_json("KEYS", "session_token:*") or []
    for k in keys_json:
        v = cli("GET", k).strip()
        if v == agent_id:
            return k.replace("session_token:", "", 1)
    return None


def trigger_a2a_deny(source_agent: str, target_host: str) -> tuple[int, str]:
    """Run an outbound A2A request from `source_agent` to `target_host` via the
    proxy. Returns (status, body). Used to trigger host-not-allowed and verify
    the alert is generated for that denial type."""
    bearer = get_bearer_for(source_agent)
    if not bearer:
        return -1, f"no session_token for {source_agent}"
    payload = json.dumps({
        "jsonrpc": "2.0", "id": "x", "method": "tasks/send",
        "params": {"id": "t", "message": {"role": "user", "parts": [{"text": "hi"}]}},
    })
    code = (
        "import urllib.request, json, sys\n"
        f"req = urllib.request.Request('http://{target_host}/', "
        f"data={payload!r}.encode(), "
        f"headers={{'X-Amaze-Bearer':'{bearer}','Content-Type':'application/json'}}, "
        "method='POST')\n"
        "proxy = urllib.request.ProxyHandler({'http':'http://amaze:8080'})\n"
        "opener = urllib.request.build_opener(proxy)\n"
        "try:\n"
        "    r = opener.open(req, timeout=10); print(r.status); print(r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code); print(e.read().decode())\n"
    )
    out = subprocess.run(
        ["docker", "exec", source_agent, "python3", "-c", code],
        capture_output=True, text=True, check=False, timeout=20,
    )
    lines = out.stdout.splitlines()
    status = int(lines[0]) if lines and lines[0].isdigit() else -1
    body = "\n".join(lines[1:])
    return status, body


def run_scenario(label: str, message: str, agents: list[str], *,
                 a2a_deny: tuple[str, str] | None = None) -> None:
    bar = "═" * 78
    print(f"\n{C['bold']}{C['yellow']}{bar}{C['reset']}")
    print(f"{C['bold']}{C['yellow']}  {label}{C['reset']}")
    print(f"{C['bold']}{C['yellow']}  prompt: {message!r}{C['reset']}")
    print(f"{C['bold']}{C['yellow']}{bar}{C['reset']}")

    t0 = int(time.time() * 1000)
    if a2a_deny:
        source, target = a2a_deny
        status, body_str = trigger_a2a_deny(source, target)
        reply = body_str
    else:
        status, body = post_chat(message)
        reply = body.get("reply") or body.get("output") or body.get("message") or str(body)

    print(f"\n  {C['bold']}HTTP{C['reset']} {status}")
    print(f"  {C['bold']}reply{C['reset']}:")
    for line in str(reply).splitlines() or [""]:
        print(f"    {line}")
    print()

    # Wait for any pending XADDs
    time.sleep(2.0)

    for agent in agents:
        recs = audit_records(agent, t0)
        header = f"  ▸ audit:{agent}  ({len(recs)} records)"
        print(f"\n{C['bold']}{C['cyan']}{header}{C['reset']}")
        if not recs:
            print(f"    {C['dim']}(empty){C['reset']}")
            continue
        kinds = [r.get("kind", "?") for r in recs]
        denied_count = sum(1 for r in recs if r.get("denied") == "true")
        unique_traces = sorted({r.get("trace_id", "") for r in recs if r.get("trace_id")})
        print(f"    {C['dim']}kinds: {kinds}  denied: {denied_count}{C['reset']}")
        print(f"    {C['dim']}unique trace_ids: {len(unique_traces)}{C['reset']}\n")
        for i, rec in enumerate(recs):
            print_record(i, rec, full=True)

    # Show shared trace_ids across the agents involved
    trace_sets = {a: {r.get("trace_id") for r in audit_records(a, t0) if r.get("trace_id")} for a in agents}
    if len(agents) >= 2:
        common = set.intersection(*trace_sets.values()) if all(trace_sets.values()) else set()
        if common:
            print(f"\n  {C['bold']}{C['green']}shared trace_ids across {agents}: {sorted(common)}{C['reset']}")
        else:
            print(f"\n  {C['bold']}{C['red']}no shared trace_ids across agents{C['reset']}")


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="Write plain-text dump to this file "
                                       "(stdout still shows colored output).")
    parser.add_argument("--scenario", choices=["10", "11", "12", "13", "all"],
                        default="all", help="Which test scenario(s) to run. "
                                            "13 = agent-not-allowed (a2a deny).")
    args = parser.parse_args()

    # Tee stdout into a buffer so we can also write a plain-text file.
    buf = io.StringIO() if args.out else None
    real_stdout = sys.stdout
    if buf is not None:
        class Tee:
            def write(self, s):
                real_stdout.write(s); buf.write(s)
            def flush(self):
                real_stdout.flush()
        sys.stdout = Tee()

    try:
        cleared = reset_session_counters()
        print(f"{C['dim']}[setup] cleared {cleared} session counter keys{C['reset']}")

        if args.scenario in ("10", "all"):
            run_scenario(
                "ST-S1.10 — Bitcoin (A2A + LLM)",
                "search for current bitcoin price",
                ["agent-sdk", "agent-sdk1"],
            )
        if args.scenario in ("11", "all"):
            run_scenario(
                "ST-S1.11 — Weather (A2A + LLM + MCP allow)",
                "search for current weather in London",
                ["agent-sdk", "agent-sdk2"],
            )
        if args.scenario in ("12", "all"):
            run_scenario(
                "ST-S1.12 — NY news (MCP tool deny)",
                "email me the current NEW YORK news",
                ["agent-sdk", "agent-sdk2"],
            )
        if args.scenario in ("13", "all"):
            run_scenario(
                "ST-S1.13 — Agent not allowed (A2A host-not-allowed)",
                "(direct A2A call: agent-sdk1 → agent-sdk2; not in allowed_agents)",
                ["agent-sdk1"],
                a2a_deny=("agent-sdk1", "agent-sdk2:9002"),
            )
    finally:
        sys.stdout = real_stdout

    if buf is not None and args.out:
        plain = ANSI_RE.sub("", buf.getvalue())
        with open(args.out, "w") as f:
            f.write(plain)
        print(f"\n[wrote {args.out}, {len(plain.splitlines())} lines]")


if __name__ == "__main__":
    main()
