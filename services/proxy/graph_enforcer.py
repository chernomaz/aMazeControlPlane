from __future__ import annotations

import json
import logging

import redis.asyncio as redis
from mitmproxy import http

from services.proxy import policy_store
from services.proxy._redis import client as redis_client
from services.proxy.deny import deny

logger = logging.getLogger(__name__)


# Atomic "reserve a slot under the loop limit" — KEYS[1] is the loop counter,
# ARGV[1] is max_loops, ARGV[2] is the TTL. Returns the new count if it would
# stay within the limit (caller may proceed and we have INCR'd as a soft
# reservation). Returns -1 if the current count is already at-or-above the
# limit (caller must deny edge_loop_exceeded).
#
# We INCR-on-reserve so concurrent requests can't both squeak past a max=1
# bound: only the FIRST INCR returns 1, the second returns 2 and we deny.
# On a 2xx response the response hook calls _advance to consume the slot;
# on a non-2xx we DECR to release it (so a 307 redirect retry doesn't burn
# a slot).
_RESERVE_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur >= tonumber(ARGV[1]) then
  return -1
end
local new = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return new
"""


# Private flow-metadata keys owned by GraphEnforcer. Underscore-prefixed so
# they're clearly internal — DO NOT read or mutate these from other addons.
# `amaze_violation` (set on a violation) IS public and intended for AuditLog.
_META_STEP_MATCH = "_graph_step_match"
_META_LOOP_KEY = "_graph_loop_key"
_META_LOOP_COUNT = "_graph_loop_count"


class GraphEnforcer:
    async def _reserve_slot(self, r: redis.Redis, loop_key: str, max_loops: int) -> int:
        """Atomically check + reserve a slot under max_loops.

        Returns the new loop count (>=1) on success, or -1 if the limit is
        already reached and the caller must deny edge_loop_exceeded.
        """
        try:
            return int(
                await r.eval(_RESERVE_LUA, 1, loop_key, str(max_loops), "86400")
            )
        except redis.RedisError:
            raise

    async def request(self, flow: http.HTTPFlow) -> None:
        """Pre-check + atomic loop reservation.

        - Verify the call's (call_type, callee_id) matches the current step.
        - Atomically INCR the step's loop counter (Lua script). If the count
          would exceed max_loops → deny edge_loop_exceeded.
        - On match + reservation, store `_graph_step_match=step_id` and
          `_graph_loop_count=count` so the response hook can:
            * advance the step pointer when count == max_loops AND status 2xx,
            * release the reservation (DECR) on non-2xx status (e.g. 307).
        """
        if flow.response is not None:
            return
        if flow.metadata.get("amaze_bypass"):
            return

        agent_id: str | None = flow.metadata.get("amaze_agent")
        sid: str | None = flow.metadata.get("amaze_session")
        kind: str | None = flow.metadata.get("amaze_kind")

        if agent_id is None or sid is None or kind is None:
            return

        try:
            policy = await policy_store.get_policy(agent_id)
        except redis.RedisError as exc:
            logger.warning("graph_enforcer: policy fetch failed for %s: %s", agent_id, exc)
            return
        if policy is None or policy.mode != "strict" or policy.graph is None:
            return

        if kind == "llm":
            # Skip LLM calls unless the graph explicitly models llm steps.
            # Backward-compatible: graphs with only tool/agent steps are unaffected.
            if not any(s.call_type == "llm" for s in policy.graph.steps):
                return
            # Skip indirect (synthesis) LLM calls — those where the messages
            # list contains a role:tool or role:function entry, meaning the LLM
            # is processing a tool result rather than responding to a fresh
            # user prompt. Only direct LLM calls are matched against the graph.
            try:
                body = json.loads(flow.request.content or b"{}")
                messages = body.get("messages", []) if isinstance(body, dict) else []
                if any(isinstance(m, dict) and m.get("role") in ("tool", "function")
                       for m in messages):
                    return
            except (ValueError, TypeError):
                return
            call_type = "llm"
            callee_id: str | None = flow.metadata.get("amaze_llm_provider")
        elif kind == "mcp":
            call_type = "tool"
            callee_id = flow.metadata.get("amaze_mcp_tool")
        elif kind == "a2a":
            call_type = "agent"
            callee_id = flow.metadata.get("amaze_target")
        else:
            return

        if callee_id is None:
            return

        step_key = f"graph:{sid}:current_step"
        violation_reason: str | None = None
        step_id: int | None = None

        try:
            r: redis.Redis = await redis_client()

            raw_step = await r.get(step_key)
            if raw_step is None:
                raw_step = str(policy.graph.start_step)
                await r.setex(step_key, 86400, raw_step)

            if raw_step == "done":
                step_id = -1
                violation_reason = "edge_loop_exceeded"
            else:
                step_id = int(raw_step)
                current_step = next(
                    (s for s in policy.graph.steps if s.step_id == step_id), None
                )
                if current_step is None:
                    violation_reason = "graph_violation"
                elif current_step.callee_id != callee_id or current_step.call_type != call_type:
                    violation_reason = "graph_violation"
                else:
                    # Atomically reserve a slot. Caps concurrent calls under
                    # max_loops and detects an N+1th attempt as edge-loop.
                    loop_key = f"graph:{sid}:step:{step_id}:loops"
                    new_count = await self._reserve_slot(r, loop_key, current_step.max_loops)
                    if new_count == -1:
                        violation_reason = "edge_loop_exceeded"
                    else:
                        flow.metadata[_META_STEP_MATCH] = step_id
                        flow.metadata[_META_LOOP_COUNT] = new_count
                        flow.metadata[_META_LOOP_KEY] = loop_key

        except redis.RedisError as exc:
            logger.warning("graph_enforcer: Redis error for session %s: %s", sid, exc)
            return

        if violation_reason is not None:
            flow.metadata["amaze_violation"] = {
                "reason": violation_reason,
                "step_id": step_id,
                "callee_id": callee_id,
            }
            if policy.on_violation == "block":
                status_code = 429 if violation_reason == "edge_loop_exceeded" else 403
                deny(flow, violation_reason, status=status_code)

    async def response(self, flow: http.HTTPFlow) -> None:
        """Finalize the slot reserved in the request hook.

        - 2xx: keep the reservation; if loop_count == max_loops, advance the
          step pointer to the first next_step (or "done" if terminal).
        - non-2xx (incl. 307 redirect retries): DECR the counter to release
          the reservation, since the call didn't logically succeed.

        Why this matters: demo-mcp emits a `307 → POST /mcp` redirect for the
        first POST to `/mcp/`. Without releasing, both POSTs would consume a
        slot and a max_loops=1 step would deny the second attempt. With the
        release, only the actual 2xx counts.
        """
        step_id = flow.metadata.get(_META_STEP_MATCH)
        loop_key = flow.metadata.get(_META_LOOP_KEY)
        loop_count = flow.metadata.get(_META_LOOP_COUNT)
        if step_id is None or loop_key is None or loop_count is None:
            return
        if flow.response is None:
            return

        agent_id = flow.metadata.get("amaze_agent")
        sid = flow.metadata.get("amaze_session")
        if not agent_id or not sid:
            return
        try:
            policy = await policy_store.get_policy(agent_id)
        except redis.RedisError as exc:
            logger.warning("graph_enforcer: policy fetch failed for %s: %s", agent_id, exc)
            return
        if policy is None or policy.graph is None:
            return
        current_step = next(
            (s for s in policy.graph.steps if s.step_id == step_id), None
        )
        if current_step is None:
            return

        is_2xx = 200 <= flow.response.status_code < 300

        try:
            r: redis.Redis = await redis_client()
            if not is_2xx:
                # Release the reservation: this attempt didn't logically
                # succeed (redirect, error, denied upstream), so don't burn
                # a graph slot. DECR is safe — we INCR'd in the request hook.
                await r.decr(loop_key)
                return

            # 2xx — slot is consumed. Advance step pointer if we've hit max.
            if loop_count >= current_step.max_loops:
                next_step = (
                    str(current_step.next_steps[0])
                    if current_step.next_steps
                    else "done"
                )
                await r.setex(f"graph:{sid}:current_step", 86400, next_step)
        except redis.RedisError as exc:
            logger.warning("graph_enforcer: Redis error finalizing step %s: %s", step_id, exc)
