"""Trace detail assembly — Sprint S4 T3.4.

Given a trace_id, walk the audit stream (`audit_query.get_trace_records`)
and project the records into the JSON shape consumed by the GUI's Trace
detail page (T3.5). The contract mirrors `TRACE_DATA` in
`services/ui_mock/index.html` (lines 1383-1430): a 3-column summary, a
sequence-step list for the SVG diagram, an edges table, and a
violations list.

Defensive parsing throughout — heterogeneous audit records, malformed
JSON, missing optional fields all degrade to empty strings / 0 rather
than raising. The Trace detail page is read-only so we never want a
single bad record to 500 the whole projection.

Public entry point: `assemble_trace(trace_id) -> dict | None`.
Returns None if the trace is unknown (caller maps to 404).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

import redis.asyncio as redis

from services.orchestrator.audit_query import AuditRecord, get_trace_records
from services.proxy import policy_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INPUT_TRUNCATE = 500
_OUTPUT_TRUNCATE = 500
_PROMPT_TRUNCATE = 500
_FINAL_ANSWER_TRUNCATE = 1000


# ---------------------------------------------------------------------------
# Tiny helpers (all best-effort, never raise)
# ---------------------------------------------------------------------------


def _safe_json(s: str) -> Any:
    """Parse JSON, return None on any failure. Never raises."""
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n]


def _ts(rec: AuditRecord) -> float:
    """Unix-seconds float from the record's `ts` string. 0.0 on failure."""
    try:
        return float(rec.get("ts", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_total_tokens(output_str: str) -> int:
    """Best-effort `usage.total_tokens` from an LLM response body. 0 on miss."""
    body = _safe_json(output_str)
    if not isinstance(body, dict):
        return 0
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get("total_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _parse_token_split(output_str: str) -> tuple[int, int, int]:
    """`(prompt_tokens, completion_tokens, total_tokens)` from LLM body. Zeros on miss."""
    body = _safe_json(output_str)
    if not isinstance(body, dict):
        return 0, 0, 0
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, 0

    def _i(k: str) -> int:
        try:
            return int(usage.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return _i("prompt_tokens"), _i("completion_tokens"), _i("total_tokens")


def _llm_response_has_tool_calls(output_str: str) -> bool:
    body = _safe_json(output_str)
    if not isinstance(body, dict):
        return False
    for choice in body.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message", {}) or {}
        if isinstance(msg, dict) and msg.get("tool_calls"):
            return True
    return False


def _llm_model(input_str: str, output_str: str) -> str:
    """Model name from the response (preferred — final, post-resolution) then
    the request body. Empty string if neither carries it."""
    body = _safe_json(output_str)
    if isinstance(body, dict) and isinstance(body.get("model"), str):
        return body["model"]
    body = _safe_json(input_str)
    if isinstance(body, dict) and isinstance(body.get("model"), str):
        return body["model"]
    return ""


def _llm_first_user_message(input_str: str) -> str:
    """First message with role=user from a chat-completions request body.
    Empty string on any miss."""
    body = _safe_json(input_str)
    if not isinstance(body, dict):
        return ""
    for m in body.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            # OpenAI vision-style content lists — flatten text parts.
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text"]
                return "".join(parts)
    return ""


def _llm_final_answer(output_str: str) -> str:
    """choices[0].message.content from an LLM response. Empty string on miss."""
    body = _safe_json(output_str)
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices", []) or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message", {}) or {}
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def _classify_violation_kind(denial_reason: str) -> str:
    """Map a denial_reason string to one of graph|policy|budget|rate."""
    r = (denial_reason or "").lower()
    if r.startswith("graph"):
        return "graph"
    if "budget" in r:
        return "budget"
    if r == "rate-limit-exceeded" or r.startswith("rate"):
        return "rate"
    if r in ("tool-not-allowed", "agent-not-allowed", "llm-not-allowed",
             "mcp-not-allowed", "not-allowed", "host-not-allowed",
             "invalid-bearer"):
        return "policy"
    return "policy"


# ---------------------------------------------------------------------------
# Main assembly
# ---------------------------------------------------------------------------


async def assemble_trace(trace_id: str) -> dict[str, Any] | None:
    """Build the full trace-detail projection for a single trace_id.

    Returns None when no audit records exist for this trace (caller -> 404).
    """
    if not trace_id:
        return None

    records = await get_trace_records(trace_id)
    if not records:
        return None

    # ----- primary agent: most common agent_id across the records ----------
    agent_counts = Counter(r.get("agent_id", "") for r in records if r.get("agent_id"))
    agent_id = agent_counts.most_common(1)[0][0] if agent_counts else ""

    # ----- timing ----------------------------------------------------------
    timestamps = [_ts(r) for r in records]
    timestamps = [t for t in timestamps if t > 0]
    if timestamps:
        started_at = min(timestamps)
        ended_at = max(timestamps)
    else:
        started_at = 0.0
        ended_at = 0.0
    duration_sec = max(0.0, ended_at - started_at)
    if duration_sec < 60:
        duration_str = f"{duration_sec:.1f}s"
    else:
        duration_str = f"{duration_sec / 60:.1f}m"

    # ----- pass/fail -------------------------------------------------------
    passed = not any(r.get("denied") == "true" for r in records)

    # ----- summary fields --------------------------------------------------
    prompt = ""
    final_answer: str | None = None
    failure_details: str | None = None

    for r in records:
        if r.get("kind") == "llm":
            prompt = _llm_first_user_message(r.get("input", ""))
            if prompt:
                break

    for r in reversed(records):
        if r.get("kind") == "llm" and r.get("denied") != "true":
            ans = _llm_final_answer(r.get("output", ""))
            if ans:
                final_answer = _truncate(ans, _FINAL_ANSWER_TRUNCATE)
                break

    for r in records:
        if r.get("denied") == "true":
            reason = r.get("denial_reason", "")
            alert = _safe_json(r.get("alert", "")) or {}
            # Prefer a structured alert message when available.
            if isinstance(alert, dict) and alert.get("message"):
                failure_details = str(alert["message"])
            elif reason:
                failure_details = reason
            else:
                failure_details = "denied"
            break

    # ----- policy snapshot -------------------------------------------------
    policy_snapshot: dict | None = None
    if agent_id:
        try:
            pol = await policy_store.get_policy(agent_id)
            if pol is not None:
                policy_snapshot = pol.model_dump(mode="json")
        except (redis.RedisError, AttributeError, ValueError) as exc:
            # Best-effort enrichment — don't fail the whole trace because
            # the policy store hiccupped or the agent is unknown.
            logger.debug("trace_detail: policy fetch for %s failed: %s",
                         agent_id, exc)

    # ----- metrics ---------------------------------------------------------
    total_tokens = 0
    llm_calls = tool_calls_count = a2a_calls = violations = 0
    tool_counter: Counter[str] = Counter()

    for r in records:
        kind = r.get("kind", "")
        if kind == "llm":
            llm_calls += 1
            total_tokens += _parse_total_tokens(r.get("output", ""))
        elif kind == "mcp":
            tool_calls_count += 1
            tool_name = r.get("tool", "")
            if tool_name:
                tool_counter[tool_name] += 1
        elif kind == "a2a":
            a2a_calls += 1
        if r.get("denied") == "true":
            violations += 1

    tool_breakdown = [
        {"tool": name, "count": count}
        for name, count in tool_counter.most_common()
    ]

    # ----- sequence_steps + edges (single pass) ----------------------------
    # `turn` increments each time we see an LLM record whose request had a
    # role=user message AND no role=tool/function messages (i.e. a fresh
    # user prompt rather than a tool-result synthesis hop). For the very
    # first record, we always start at turn 1.
    sequence_steps: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    violations_list: list[dict[str, Any]] = []

    turn = 0
    index_within_turn = 0
    last_to = "user"

    for i, r in enumerate(records):
        kind = r.get("kind", "")
        denied = r.get("denied") == "true"
        alert_obj = _safe_json(r.get("alert", "")) or {}
        if not isinstance(alert_obj, dict):
            alert_obj = {}

        # ---- turn calculation (LLM-driven) -------------------------------
        is_new_turn = False
        if kind == "llm":
            req_body = _safe_json(r.get("input", ""))
            has_user = False
            has_tool_input = False
            if isinstance(req_body, dict):
                for m in req_body.get("messages", []) or []:
                    if not isinstance(m, dict):
                        continue
                    role = m.get("role")
                    if role == "user":
                        has_user = True
                    if role in ("tool", "function"):
                        has_tool_input = True
            if has_user and not has_tool_input:
                is_new_turn = True
        if i == 0 and turn == 0:
            is_new_turn = True
        if is_new_turn:
            turn += 1
            index_within_turn = 0
        else:
            index_within_turn += 1

        # ---- sequence step ----------------------------------------------
        if kind == "llm":
            provider = r.get("target", "") or "llm"
            model = _llm_model(r.get("input", ""), r.get("output", ""))
            to_node = f"{provider}/{model}" if model else provider
            label = to_node
        elif kind == "mcp":
            tool_name = r.get("tool", "") or r.get("target", "") or "tool"
            to_node = f"tool:{tool_name}"
            label = tool_name
        elif kind == "a2a":
            tgt = r.get("target", "") or "agent"
            to_node = f"agent:{tgt}"
            label = tgt
        else:
            to_node = r.get("target", "") or "?"
            label = to_node

        from_node = "user" if i == 0 else last_to
        step_status = "failed" if denied else "real"
        sequence_steps.append({
            "from": from_node,
            "to": to_node,
            "label": label,
            "status": step_status,
        })
        last_to = to_node

        # ---- edge row ----------------------------------------------------
        in_tok = out_tok = tot_tok = 0
        model_for_edge: str | None = None
        indirect_flag = r.get("indirect") == "true"
        if kind == "llm":
            in_tok, out_tok, tot_tok = _parse_token_split(r.get("output", ""))
            m = _llm_model(r.get("input", ""), r.get("output", ""))
            model_for_edge = m if m else None
            # Belt-and-braces: prefer the indirect bool stored at write
            # time, fall back to re-detecting from the response body if
            # the field is missing on older entries.
            if "indirect" not in r:
                indirect_flag = _llm_response_has_tool_calls(r.get("output", ""))

        if kind == "llm":
            edge_name = model_for_edge or r.get("target", "") or "llm"
        elif kind == "mcp":
            edge_name = r.get("tool", "") or r.get("target", "") or "tool"
        elif kind == "a2a":
            edge_name = r.get("target", "") or "agent"
        else:
            edge_name = r.get("target", "") or kind or "unknown"

        if denied:
            edge_status = "denied"
        elif r.get("denial_reason"):
            edge_status = "error"
        else:
            edge_status = "ok"

        # source: the conventional caller. For the first record we treat
        # the user as the originator; afterwards the agent is. We don't
        # have request/response correlation in the audit stream, so
        # duration_ms is best-effort 0.
        if i == 0:
            edge_source = "user"
        elif kind == "llm":
            edge_source = "agent"
        else:
            edge_source = "llm"

        edges.append({
            "turn": turn,
            "index": index_within_turn,
            "ts": _ts(r),
            "type": kind or "unknown",
            "name": edge_name,
            "indirect": indirect_flag,
            "source": edge_source,
            "model": model_for_edge,
            "duration_ms": 0,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": tot_tok,
            "status": edge_status,
            "input": _truncate(r.get("input", ""), _INPUT_TRUNCATE),
            "output": _truncate(r.get("output", ""), _OUTPUT_TRUNCATE),
        })

        # ---- violation row (denied, OR alerted-mode pass-with-alert) ----
        is_alerted_only = (not denied) and bool(alert_obj)
        if denied or is_alerted_only:
            v_kind = _classify_violation_kind(r.get("denial_reason", ""))
            if is_alerted_only:
                # Trust the alert payload's `type` field for classification
                # in pass-with-alert mode, since denial_reason is empty.
                a_type = str(alert_obj.get("type", "")) if alert_obj else ""
                if a_type:
                    v_kind = _classify_violation_kind(a_type)
            if kind == "mcp":
                v_name = r.get("tool", "") or r.get("target", "") or ""
            elif kind == "a2a":
                v_name = r.get("target", "") or ""
            elif kind == "llm":
                v_name = r.get("target", "") or ""
            else:
                v_name = r.get("target", "") or ""
            violations_list.append({
                "kind": v_kind,
                "name": v_name,
                "turn": turn,
                "index": index_within_turn,
                "status": "blocked" if denied else "alerted",
                "details": alert_obj if isinstance(alert_obj, dict) else {},
            })

    # ----- finalize --------------------------------------------------------
    return {
        "trace_id": trace_id,
        "title": trace_id[:8] if trace_id else "",
        "passed": passed,
        "duration": duration_str,
        "agent_id": agent_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "summary": {
            "prompt": _truncate(prompt, _PROMPT_TRUNCATE),
            "final_answer": final_answer,
            "failure_details": failure_details,
            "policy_snapshot": policy_snapshot,
        },
        "metrics": {
            "total_tokens": total_tokens,
            "llm_calls": llm_calls,
            "tool_calls": tool_calls_count,
            "a2a_calls": a2a_calls,
            "violations": violations,
            "tool_breakdown": tool_breakdown,
        },
        "sequence_steps": sequence_steps,
        "edges": edges,
        "violations_list": violations_list,
    }
