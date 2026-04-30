"""ST-S1.14 — Bitcoin scenario under STRICT mode with execution graph.

Hypothesis (from the user): a strict graph that reflects the real-world flow
will trip up because the proxy emits TWO audit events per MCP `tools/call`
(one for the request, one for the SSE response), and both carry tool=web_search.
With max_loops=1 on the web_search step, the second occurrence triggers
graph_violation (the graph has already advanced to the next step).

What this script does:
  1. Read /app/config/policies.yaml from the live container.
  2. Patch agent-sdk to mode=strict with this graph:
        start_step: 1
        steps:
          - 1  tool/web_search   max_loops:1   next:[2]
          - 2  agent/agent-sdk1  max_loops:1   next:[]   (terminal)
     (LLM is not modelled per spec.)
  3. Restart proxy so the new policy loads.
  4. Reset per-session counters and trace contexts so we have a clean run.
  5. POST "search for current bitcoin price" to agent-sdk:8080/chat.
  6. Dump audit:agent-sdk and audit:agent-sdk1.
  7. Restore the original policies.yaml and restart proxy.

Run:
  /home/ubuntu/venv/bin/python tests/dump_audit_strict_graph.py
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import time

import httpx

PLATFORM = "amaze-platform"
AGENT_SDK = "http://localhost:8090"
HTTP_TIMEOUT = 90.0
POLICY_PATH = "/app/config/policies.yaml"
# Backup file kept inside the container so a SIGKILL between mutate and
# restore leaves a recovery breadcrumb. Next run detects the .bak and
# restores the original before doing anything else.
POLICY_BACKUP = "/app/config/policies.yaml.pre_strict.bak"
OUT_FILE = "tests/audit_dump_st_s1_14_strict.txt"


# ── Color (stripped on file write) ──────────────────────────────────────────
C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def cli(*args: str) -> str:
    out = subprocess.run(
        ["docker", "exec", PLATFORM, *args],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker exec failed: {out.stderr.strip()}")
    return out.stdout


def cli_json(*args: str):
    out = subprocess.run(
        ["docker", "exec", PLATFORM, "redis-cli", "--json", *args],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


# ── Policy mutation (crash-safe) ────────────────────────────────────────────
def _container_file_exists(path: str) -> bool:
    out = subprocess.run(
        ["docker", "exec", PLATFORM, "test", "-f", path],
        capture_output=True, text=True, check=False, timeout=10,
    )
    return out.returncode == 0


def recover_from_prior_crash() -> bool:
    """If a previous run crashed before restoring, the .bak file still
    exists. Restore it before doing anything else. Returns True if recovery
    happened (caller may want to log)."""
    if _container_file_exists(POLICY_BACKUP):
        cli("mv", POLICY_BACKUP, POLICY_PATH)
        return True
    return False


def read_policy_file() -> str:
    return cli("cat", POLICY_PATH)


def write_policy_file_atomic(content: str) -> None:
    """Write the new policy via tempfile + atomic mv. The backup is taken
    BEFORE mutation so a crash between mutation and finally-restore is
    recoverable from the .bak on next run.
    """
    # 1. Backup (cp -p preserves perms/timestamps).
    cli("cp", "-p", POLICY_PATH, POLICY_BACKUP)
    # 2. Stage new content into a tempfile.
    tmp = POLICY_PATH + ".new"
    proc = subprocess.run(
        ["docker", "exec", "-i", PLATFORM, "tee", tmp],
        input=content, text=True, capture_output=True, check=False, timeout=15,
    )
    if proc.returncode != 0:
        # Don't leave the .bak around if we couldn't even stage.
        cli("rm", "-f", POLICY_BACKUP, tmp)
        raise RuntimeError(f"failed to stage tmp policy: {proc.stderr}")
    # 3. Atomic replace.
    cli("mv", tmp, POLICY_PATH)


def restore_policy_file() -> None:
    """Restore from the backup taken before mutation. Idempotent: if no
    backup exists (shouldn't happen but defensive), do nothing."""
    if _container_file_exists(POLICY_BACKUP):
        cli("mv", POLICY_BACKUP, POLICY_PATH)


STRICT_BLOCK = """
  agent-sdk:
    name: agent-sdk
    max_tokens_per_turn: 10000
    max_tool_calls_per_turn: 20
    max_agent_calls_per_turn: 5
    allowed_llm_providers: [openai]
    token_rate_limits:
      - window: 10m
        max_tokens: 5000
      - window: 1h
        max_tokens: 20000
    on_budget_exceeded: block
    on_violation: block
    mode: strict
    allowed_tools: [web_search]
    allowed_agents: [agent-sdk1, agent-sdk2]
    graph:
      start_step: 1
      steps:
        - step_id: 1
          call_type: tool
          callee_id: web_search
          max_loops: 1
          next_steps: [2]
        - step_id: 2
          call_type: agent
          callee_id: agent-sdk1
          max_loops: 1
          next_steps: []

"""


def patch_agent_sdk(original: str) -> str:
    """Replace agent-sdk's policy block with the strict variant."""
    # Match from `  agent-sdk:` up to the next top-level `  agent-` block start.
    pattern = re.compile(
        r"(^  agent-sdk:\n.*?)(?=^  agent-sdk1:)",
        re.DOTALL | re.MULTILINE,
    )
    if not pattern.search(original):
        raise RuntimeError("Could not locate agent-sdk policy block to patch")
    return pattern.sub(STRICT_BLOCK.lstrip("\n"), original)


def restart_proxy() -> None:
    subprocess.run(
        ["docker", "exec", PLATFORM, "bash", "-c",
         'for p in /proc/[0-9]*; do c=$(cat $p/comm 2>/dev/null); '
         'if [ "$c" = "mitmdump" ]; then kill $(basename $p); fi; done'],
        capture_output=True, text=True, check=False, timeout=10,
    )
    # supervisord respawns; wait for it to come back
    for _ in range(15):
        time.sleep(1)
        out = subprocess.run(
            ["docker", "exec", PLATFORM, "bash", "-c",
             'for p in /proc/[0-9]*; do c=$(cat $p/comm 2>/dev/null); echo "$c"; done | grep -c mitmdump'],
            capture_output=True, text=True, check=False,
        )
        if out.stdout.strip().isdigit() and int(out.stdout.strip()) > 0:
            time.sleep(2)  # give it time to bind
            return
    raise RuntimeError("proxy did not respawn within 15s")


# ── Audit access ────────────────────────────────────────────────────────────
def reset_session_state() -> None:
    for pattern in ("session:*", "trace_context:*", "graph:*"):
        keys = cli_json("KEYS", pattern) or []
        if keys:
            subprocess.run(
                ["docker", "exec", PLATFORM, "redis-cli", "DEL", *keys],
                capture_output=True, check=False, timeout=10,
            )


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


# ── Pretty print ────────────────────────────────────────────────────────────
def fmt_kind(rec):
    kind = rec.get("kind", "?")
    color = {"llm": C["blue"], "mcp": C["cyan"], "a2a": C["magenta"]}.get(kind, C["yellow"])
    suffix = ""
    if kind == "llm":
        if rec.get("indirect") == "true":
            suffix = f"{C['magenta']}*{C['reset']}"
        elif rec.get("has_tool_calls_input") == "true":
            suffix = f"{C['green']}↩{C['reset']}"
    return f"{color}{kind:<6}{C['reset']}{suffix}"


def fmt_status(rec):
    if rec.get("denied", "false") == "true":
        return f"{C['red']}DENIED{C['reset']}"
    return f"{C['green']}allow {C['reset']}"


def print_record(idx, rec):
    target = rec.get("target") or "—"
    tool = rec.get("tool") or "—"
    print(f"  {C['bold']}[{idx:>2}]{C['reset']} {fmt_kind(rec)} {fmt_status(rec)} "
          f"target={C['dim']}{target}{C['reset']} tool={C['dim']}{tool}{C['reset']}")
    print(f"      {C['dim']}trace_id   {C['reset']}: {rec.get('trace_id') or '—'}")
    print(f"      {C['dim']}span_id    {C['reset']}: {rec.get('span_id') or '—'}")
    if rec.get("denial_reason"):
        print(f"      {C['red']}denial     {C['reset']}: {rec.get('denial_reason')}")
    if rec.get("alert"):
        print(f"      {C['yellow']}alert      {C['reset']}: {rec.get('alert')}")
    if rec.get("kind") == "llm":
        ind = rec.get("indirect", "false") == "true"
        synth = rec.get("has_tool_calls_input", "false") == "true"
        if ind or synth:
            tags = []
            if ind: tags.append("indirect (returned tool_calls)")
            if synth: tags.append("synthesis (input had tool results)")
            print(f"      {C['cyan']}llm-flags  {C['reset']}: {', '.join(tags)}")
    print(f"      {C['dim']}input      {C['reset']}:")
    for line in (rec.get("input", "") or "(empty)").splitlines() or [""]:
        print(f"        {line}")
    print(f"      {C['dim']}output     {C['reset']}:")
    for line in (rec.get("output", "") or "(empty)").splitlines() or [""]:
        print(f"        {line}")
    print()


def post_chat(message: str):
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.post(f"{AGENT_SDK}/chat", json={"message": message})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text}


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    # Tee stdout to a buffer for plain-text file output
    buf = io.StringIO()
    real = sys.stdout

    class Tee:
        def write(self, s): real.write(s); buf.write(s)
        def flush(self): real.flush()
    sys.stdout = Tee()

    if recover_from_prior_crash():
        print(f"{C['yellow']}[setup] Recovered policies.yaml from a prior crashed run.{C['reset']}")
        print(f"{C['dim']}[setup] Restarting proxy to load restored policy...{C['reset']}")
        restart_proxy()

    print(f"{C['dim']}[setup] Reading current /app/config/policies.yaml...{C['reset']}")
    original = read_policy_file()
    print(f"{C['dim']}[setup] Backing up + patching agent-sdk to STRICT...{C['reset']}")
    patched = patch_agent_sdk(original)

    try:
        write_policy_file_atomic(patched)
        print(f"{C['dim']}[setup] Restarting proxy...{C['reset']}")
        restart_proxy()
        print(f"{C['dim']}[setup] Resetting Redis session state...{C['reset']}")
        reset_session_state()

        print(f"\n{C['bold']}{C['yellow']}{'═'*78}{C['reset']}")
        print(f"{C['bold']}{C['yellow']}  ST-S1.14 — Bitcoin under STRICT execution graph{C['reset']}")
        print(f"{C['bold']}{C['yellow']}  graph: tool/web_search → agent/agent-sdk1 (max_loops=1 each){C['reset']}")
        print(f"{C['bold']}{C['yellow']}{'═'*78}{C['reset']}\n")

        t0 = int(time.time() * 1000)
        status, body = post_chat("search for current bitcoin price")
        reply = body.get("reply") or body.get("output") or body.get("message") or str(body)
        print(f"  {C['bold']}HTTP{C['reset']} {status}")
        print(f"  {C['bold']}reply{C['reset']}:")
        for line in str(reply).splitlines() or [""]:
            print(f"    {line}")
        print()

        time.sleep(2.0)

        for agent in ("agent-sdk", "agent-sdk1"):
            recs = audit_records(agent, t0)
            print(f"\n{C['bold']}{C['cyan']}  ▸ audit:{agent}  ({len(recs)} records){C['reset']}")
            kinds = [r.get("kind") for r in recs]
            denied = sum(1 for r in recs if r.get("denied") == "true")
            violation_records = [r for r in recs if r.get("denial_reason") in ("graph_violation", "edge_loop_exceeded")]
            print(f"    {C['dim']}kinds: {kinds}  denied: {denied}  graph_violations: {len(violation_records)}{C['reset']}\n")
            if not recs:
                print(f"    {C['dim']}(empty){C['reset']}")
                continue
            for i, rec in enumerate(recs):
                print_record(i, rec)

    finally:
        print(f"\n{C['dim']}[teardown] Restoring original policies.yaml from backup...{C['reset']}")
        try:
            restore_policy_file()
            print(f"{C['dim']}[teardown] Restarting proxy...{C['reset']}")
            restart_proxy()
        except Exception as e:
            print(f"{C['red']}[teardown] FAILED: {e}{C['reset']}", file=real)
            print(f"{C['red']}[teardown] /app/config/policies.yaml may be in an inconsistent "
                  f"state. Backup at {POLICY_BACKUP} (if it still exists). "
                  f"Re-run this script to auto-recover.{C['reset']}", file=real)
            raise

    sys.stdout = real

    # Write plain-text file
    plain = ANSI_RE.sub("", buf.getvalue())
    with open(OUT_FILE, "w") as f:
        f.write(plain)
    print(f"\n[wrote {OUT_FILE}, {len(plain.splitlines())} lines]")


if __name__ == "__main__":
    main()
