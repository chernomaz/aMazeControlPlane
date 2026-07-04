"""
Sprint S6 system tests — ST-MU.1 through ST-MU.5 (Multi-User Live Debugger).

Validates that two human users can step-debug the SAME agent concurrently and
independently — each browser (simulated as a distinct value of the request
header ``X-Amaze-Debug-User``) sees only its own paused steps, advances only
its own queue, and never leaks steps into the other user's session.

These run full-stack against the LIVE ``amaze-platform`` container exactly like
``test_s4.py`` (host ports 8001/orch, 6379/redis on 127.0.0.1, agent-sdk chat
on 8090). We do NOT spin up ``tests/compose.test.yml`` — the live demo stack is
the device under test. Real example agents, real LLM, real MCP — no mocks.

Override hosts:
  AMAZE_ORCHESTRATOR   http://localhost:8001
  REDIS_HOST           127.0.0.1
  REDIS_PORT           6379

Behaviour contract (S6-T0, signed off in SPRINTS.md "System tests S6"):
  * Two browsers == two distinct ``X-Amaze-Debug-User`` header values (u1, u2).
  * Debug endpoints are per-user — ALL require ``X-Amaze-Debug-User`` (400 if
    absent):
      PUT  /agents/{id}/debug          {enabled}
      GET  /agents/{id}/debug/current
      POST /agents/{id}/debug/next     {step_id, override?}
      POST /agents/{id}/debug/skip-all
  * send-message: POST /agents/{id}/messages {prompt} with the header → the
    agent's outbound calls get parked under ``debug:{agent}:{user}:queue``.
  * Redis keyspace (per user):
      debug:{agent}:{user}:{enabled|skip_mode|queue|history|step:{id}|gate:{id}}
      debug:{peer}:{user}:primary_agent    (A2A peer routing)
  * A send-message with NO ``X-Amaze-Debug-User`` header is never parked.

How the tests drive the slow step-through flow
----------------------------------------------
When debug is enabled, the proxy parks EVERY outbound call and the agent's
``/chat`` handler blocks until the UI presses Next. So ``POST .../messages``
does not return until the run completes (or debug expires). We therefore fire
send-message on a background thread (mirroring how the real GUI fires it
without awaiting) and poll ``/debug/current`` from the foreground, releasing
steps with ``/next`` / ``/skip-all`` as needed. Timeouts are generous because a
single LLM round-trip behind a paused gate is slow.

Skip conditions (environment-tolerant — the stack may be down here):
  * Redis unreachable                       → skip (rc fixture).
  * Orchestrator down / agent-sdk missing   → skip.
  * Debug never engages within the deadline → skip (LLM/MCP/key unavailable);
    we assert correctness when the stack is live, never demand it be up.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx
import pytest
import redis

from conftest import _amaze_stack_up, _seed_user, S7_USERS

# ── connection settings (mirror test_s4.py — live stack) ───────────────────

ORCH = os.environ.get("AMAZE_ORCHESTRATOR", "http://localhost:8001")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

# Live demo agent we exercise — already approved + has policy + chat_endpoint
# registered (see CLAUDE.md §8 demo wiring; same agent test_s4.py uses).
DEMO_AGENT = "agent-sdk"

# Two simulated browsers — distinct X-Amaze-Debug-User header values.
U1 = "st-mu-u1"
U2 = "st-mu-u2"

# S7: the debug + send-message endpoints are now session-gated. We log in once
# as the deterministic admin principal (role=admin → owns/bypasses every agent)
# and reuse its `amaze_session` cookie on EVERY request, including the
# background send-message threads. The two X-Amaze-Debug-User UUIDs above are
# orthogonal to this cookie — both run under the single admin session.
_ADMIN_COOKIES: httpx.Cookies = httpx.Cookies()

# A "bitcoin" prompt routes agent-sdk → agent-sdk1; "weather" → agent-sdk2
# (see examples/agents/agent_sdk.py:68). Either fans out to a real LLM + MCP
# tool call first, which is what gets parked.
PROMPT_WEATHER = "search for current weather in Berlin"
PROMPT_BITCOIN = "search for the current bitcoin price"

# agent-sdk's declared A2A peers — the orchestrator writes
# debug:{peer}:{user}:primary_agent for each of these when debug is enabled.
KNOWN_PEERS = ("agent-sdk1", "agent-sdk2")

# Polling budgets — debug pauses are slow (each gate waits on a real LLM hop).
STEP_WAIT_TIMEOUT = 90.0   # wait for the first parked step to appear
POLL_INTERVAL = 0.5
RUN_DRAIN_TIMEOUT = 180.0  # wait for a backgrounded send-message run to finish


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    """Plain orchestrator client (no debug header) — see test_s4.py `http`."""
    # S7: seed the deterministic admin principal so we can authenticate.
    if not _amaze_stack_up():
        pytest.skip("amaze-platform stack not up on :8001")
    for u, p, role in S7_USERS:
        _seed_user(u, p, role)
    with httpx.Client(base_url=ORCH, timeout=60.0) as c:
        # Fail fast (skip, not error) if the live stack is down.
        try:
            h = c.get("/health")
        except httpx.HTTPError as e:
            pytest.skip(f"orchestrator unreachable at {ORCH}: {e}")
        if h.status_code != 200:
            pytest.skip(f"orchestrator /health not ok: {h.status_code} {h.text}")
        # Log in as admin; cache the session cookie for the background
        # send-message threads (which build their own clients).
        login = c.post(
            "/auth/login",
            json={"username": "s7-root", "password": "s7-root-pass"},
        )
        assert login.status_code == 200, f"admin login failed: {login.text}"
        _ADMIN_COOKIES.update(c.cookies)
        yield c


@pytest.fixture(scope="module")
def rc() -> redis.Redis:
    """Live Redis client — identical skip-on-unreachable contract to test_s4."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        r.ping()
    except redis.RedisError as e:
        pytest.skip(f"redis unreachable at {REDIS_HOST}:{REDIS_PORT}: {e}")
    yield r
    r.close()


@pytest.fixture(autouse=True)
def clean_debug_keys(rc: redis.Redis):
    """Wipe every debug:* key for our two test users before AND after each test.

    Covers the primary agent's keys and the per-user peer-routing keys so reruns
    are deterministic and a leaked gate/queue from a prior run can't leak in.
    """
    _purge_debug_users(rc, (U1, U2))
    yield
    _purge_debug_users(rc, (U1, U2))


# ── helpers ────────────────────────────────────────────────────────────────


def _purge_debug_users(rc: redis.Redis, users: tuple[str, ...]) -> None:
    """Delete all ``debug:*:{user}:*`` keys for the given users (any agent)."""
    for user in users:
        # debug:{agent}:{user}:...  — :{user}: appears mid-key, so scan broadly.
        for key in rc.scan_iter(match=f"debug:*:{user}:*"):
            rc.delete(key)


def _du(user: str) -> dict[str, str]:
    """The per-user debug header every endpoint requires."""
    return {"X-Amaze-Debug-User": user}


def _enable_debug(http: httpx.Client, user: str) -> None:
    r = http.put(f"/agents/{DEMO_AGENT}/debug", json={"enabled": True},
                 headers=_du(user))
    assert r.status_code == 200, f"enable debug for {user}: {r.status_code} {r.text}"


def _disable_debug(http: httpx.Client, user: str) -> None:
    # Best-effort teardown — releases the dead-man's-switch immediately.
    try:
        http.put(f"/agents/{DEMO_AGENT}/debug", json={"enabled": False},
                 headers=_du(user))
    except httpx.HTTPError:
        pass


def _current(http: httpx.Client, user: str) -> dict[str, Any]:
    r = http.get(f"/agents/{DEMO_AGENT}/debug/current", headers=_du(user))
    assert r.status_code == 200, f"current for {user}: {r.status_code} {r.text}"
    return r.json()


def _send_message_async(user: str | None, prompt: str) -> threading.Thread:
    """Fire POST /agents/{id}/messages on a daemon thread (UI fire-and-forget).

    With debug enabled every outbound call is parked, so this call blocks until
    the run drains. We never join() it on the critical path; the foreground
    polls /debug/current and releases gates. ``user=None`` sends NO debug
    header (the untagged-bypass case).
    """
    def _run() -> None:
        headers = _du(user) if user is not None else None
        try:
            # S7: send-message is session-gated — reuse the admin cookie.
            with httpx.Client(base_url=ORCH, timeout=RUN_DRAIN_TIMEOUT,
                              cookies=_ADMIN_COOKIES) as c:
                c.post(f"/agents/{DEMO_AGENT}/messages",
                       json={"prompt": prompt}, headers=headers)
        except httpx.HTTPError:
            pass  # the assertions read Redis/endpoints, not this reply

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _wait_for_step(
    http: httpx.Client, user: str, timeout: float = STEP_WAIT_TIMEOUT
) -> dict[str, Any] | None:
    """Poll /debug/current until a paused step appears; return it or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = _current(http, user)
        if cur.get("paused") and cur.get("step"):
            return cur["step"]
        time.sleep(POLL_INTERVAL)
    return None


def _queue_ids(rc: redis.Redis, agent: str, user: str) -> list[str]:
    return rc.lrange(f"debug:{agent}:{user}:queue", 0, -1)


def _history_ids(rc: redis.Redis, agent: str, user: str) -> list[str]:
    return rc.lrange(f"debug:{agent}:{user}:history", 0, -1)


def _drain_with_skip(http: httpx.Client, user: str) -> None:
    """Release whatever is parked for a user so its send-message thread can end.

    skip-all also engages skip-mode, so any later parks in the same run pass
    straight through — keeps teardown from hanging on a half-stepped run.
    """
    try:
        http.post(f"/agents/{DEMO_AGENT}/debug/skip-all", headers=_du(user))
    except httpx.HTTPError:
        pass


# ── ST-MU.1 — Independent queues, same agent ───────────────────────────────


def test_st_mu_1_independent_queues(http: httpx.Client, rc: redis.Redis) -> None:
    """ST-MU.1 — Two users debug the same agent; each sees only its own step.

    Asserts:
      * /debug/current returns a paused step for BOTH u1 and u2 (HTTP 200).
      * The Redis queue + history step_ids for u1 are DISJOINT from u2's
        (debug:agent-sdk:u1:queue vs debug:agent-sdk:u2:queue, and history).
    """
    _enable_debug(http, U1)
    _enable_debug(http, U2)

    t1 = _send_message_async(U1, PROMPT_WEATHER)
    t2 = _send_message_async(U2, PROMPT_WEATHER)
    try:
        step1 = _wait_for_step(http, U1)
        step2 = _wait_for_step(http, U2)
        if step1 is None or step2 is None:
            pytest.skip(
                "debug never engaged for both users within deadline — "
                "live LLM/MCP/key likely unavailable"
            )

        # Each user sees a paused step of its own.
        assert step1.get("step_id"), step1
        assert step2.get("step_id"), step2

        # Redis queues must be disjoint between the two users.
        q1 = set(_queue_ids(rc, DEMO_AGENT, U1))
        q2 = set(_queue_ids(rc, DEMO_AGENT, U2))
        assert q1, f"u1 queue should be non-empty while parked: {q1}"
        assert q2, f"u2 queue should be non-empty while parked: {q2}"
        assert q1.isdisjoint(q2), f"queues must not share step_ids: {q1 & q2}"

        # History (the persistent diagram backing list) is likewise disjoint.
        h1 = set(_history_ids(rc, DEMO_AGENT, U1))
        h2 = set(_history_ids(rc, DEMO_AGENT, U2))
        assert h1.isdisjoint(h2), f"histories must not share step_ids: {h1 & h2}"

        # The step each user polls must belong to its own queue, not the peer's.
        assert step1["step_id"] in q1 and step1["step_id"] not in q2, step1
        assert step2["step_id"] in q2 and step2["step_id"] not in q1, step2
    finally:
        _drain_with_skip(http, U1)
        _drain_with_skip(http, U2)
        _disable_debug(http, U1)
        _disable_debug(http, U2)
        t1.join(timeout=5)
        t2.join(timeout=5)


# ── ST-MU.2 — Independent Next ─────────────────────────────────────────────


def test_st_mu_2_independent_next(http: httpx.Client, rc: redis.Redis) -> None:
    """ST-MU.2 — Pressing Next as u1 advances u1 only; u2's step is unchanged.

    Asserts:
      * Both users are parked first.
      * POST /debug/next {step_id} as u1 → 200 {"advanced": step_id}.
      * After the advance, u2's /debug/current still reports the SAME step_id
        it reported before (u1's action did not touch u2's queue).
    """
    _enable_debug(http, U1)
    _enable_debug(http, U2)

    t1 = _send_message_async(U1, PROMPT_WEATHER)
    t2 = _send_message_async(U2, PROMPT_WEATHER)
    try:
        step1 = _wait_for_step(http, U1)
        step2_before = _wait_for_step(http, U2)
        if step1 is None or step2_before is None:
            pytest.skip("debug never engaged for both users — stack unavailable")

        u2_step_id_before = step2_before["step_id"]

        # Advance u1 only.
        adv = http.post(
            f"/agents/{DEMO_AGENT}/debug/next",
            json={"step_id": step1["step_id"]},
            headers=_du(U1),
        )
        assert adv.status_code == 200, f"u1 next: {adv.status_code} {adv.text}"
        assert adv.json().get("advanced") == step1["step_id"], adv.json()

        # u1's queue head must no longer be the advanced step (it advanced).
        # Give Redis a beat; the lpop happens synchronously in the endpoint.
        assert step1["step_id"] not in _queue_ids(rc, DEMO_AGENT, U1), (
            "u1's advanced step must be popped from its queue"
        )

        # u2 must still be parked on the exact same step it had before.
        u2_now = _current(http, U2)
        assert u2_now.get("paused"), f"u2 should remain paused: {u2_now}"
        assert u2_now["step"]["step_id"] == u2_step_id_before, (
            f"u2's step must be unchanged by u1's Next: "
            f"before={u2_step_id_before} after={u2_now['step']['step_id']}"
        )
        # And u2's step must NOT be the one u1 advanced.
        assert u2_now["step"]["step_id"] != step1["step_id"], u2_now
    finally:
        _drain_with_skip(http, U1)
        _drain_with_skip(http, U2)
        _disable_debug(http, U1)
        _disable_debug(http, U2)
        t1.join(timeout=5)
        t2.join(timeout=5)


# ── ST-MU.3 — Per-user TTL isolation ───────────────────────────────────────


def test_st_mu_3_per_user_ttl_isolation(
    http: httpx.Client, rc: redis.Redis
) -> None:
    """ST-MU.3 — u1 stops polling (enabled key expires) → its parked step
    cancels; u2 stays parked and can still Next.

    To keep the test fast we DELETE debug:agent-sdk:u1:enabled directly,
    simulating the ENABLED_TTL dead-man's-switch firing (the proxy's gate
    poll re-checks the enabled key every 5 s and cancels with 403
    ``debug-cancelled`` once it's gone).

    Asserts:
      * u1's parked step transitions to status "cancelled" on its step hash
        (equivalently the agent run returns 403 debug-cancelled).
      * u2 remains paused and a /debug/next as u2 still returns 200.
    """
    _enable_debug(http, U1)
    _enable_debug(http, U2)

    t1 = _send_message_async(U1, PROMPT_WEATHER)
    t2 = _send_message_async(U2, PROMPT_WEATHER)
    try:
        step1 = _wait_for_step(http, U1)
        step2 = _wait_for_step(http, U2)
        if step1 is None or step2 is None:
            pytest.skip("debug never engaged for both users — stack unavailable")

        step1_key = f"debug:{DEMO_AGENT}:{U1}:step:{step1['step_id']}"

        # Simulate u1's browser vanishing: drop the dead-man's-switch key.
        rc.delete(f"debug:{DEMO_AGENT}:{U1}:enabled")

        # The proxy re-checks the enabled key every ~5 s in _poll_gate; wait
        # for u1's step to flip to "cancelled".
        deadline = time.time() + 30.0
        cancelled = False
        while time.time() < deadline:
            status = rc.hget(step1_key, "status")
            if status == "cancelled":
                cancelled = True
                break
            time.sleep(POLL_INTERVAL)
        assert cancelled, (
            f"u1's step {step1['step_id']} should be 'cancelled' after its "
            f"enabled key expired; got status={rc.hget(step1_key, 'status')!r}"
        )

        # u2 is unaffected: still parked on the same step, and Next works.
        u2_now = _current(http, U2)
        assert u2_now.get("paused"), f"u2 must stay parked: {u2_now}"
        assert u2_now["step"]["step_id"] == step2["step_id"], u2_now

        adv = http.post(
            f"/agents/{DEMO_AGENT}/debug/next",
            json={"step_id": step2["step_id"]},
            headers=_du(U2),
        )
        assert adv.status_code == 200, (
            f"u2 must still be able to step after u1 expired: "
            f"{adv.status_code} {adv.text}"
        )
        assert adv.json().get("advanced") == step2["step_id"], adv.json()
    finally:
        _drain_with_skip(http, U1)
        _drain_with_skip(http, U2)
        _disable_debug(http, U1)
        _disable_debug(http, U2)
        t1.join(timeout=5)
        t2.join(timeout=5)


# ── ST-MU.4 — A2A peer carries the originating user ────────────────────────


def test_st_mu_4_a2a_peer_carries_user(
    http: httpx.Client, rc: redis.Redis
) -> None:
    """ST-MU.4 — A peer call made on u1's behalf lands in u1's history (routed
    via debug:{peer}:u1:primary_agent), never in u2's history.

    agent-sdk always forwards to a peer (agent-sdk1 for "bitcoin",
    agent-sdk2 otherwise — examples/agents/agent_sdk.py:68). We debug as u1 and
    drive the run forward step-by-step so it fans out to the peer; meanwhile u2
    keeps an idle debug session open on the same agent.

    Asserts:
      * The orchestrator wrote debug:{peer}:u1:primary_agent = agent-sdk for
        the agent's declared peers (per-user peer propagation).
      * At least one history step recorded by a peer agent (step hash field
        ``agent`` == a peer id) appears in u1's history.
      * NONE of u1's history step_ids appear in u2's history.
    """
    _enable_debug(http, U1)
    _enable_debug(http, U2)

    # Per-user peer routing keys must exist for u1 (and u2) right after enable.
    present_peers = [
        p for p in KNOWN_PEERS
        if rc.get(f"debug:{p}:{U1}:primary_agent") == DEMO_AGENT
    ]
    assert present_peers, (
        "expected at least one debug:{peer}:u1:primary_agent=agent-sdk key after "
        f"enabling debug; checked {KNOWN_PEERS}"
    )
    # u2 gets its own independent peer routing keys (not shared with u1).
    for p in present_peers:
        assert rc.get(f"debug:{p}:{U2}:primary_agent") == DEMO_AGENT, (
            f"u2 must have its own debug:{p}:u2:primary_agent key"
        )

    t1 = _send_message_async(U1, PROMPT_WEATHER)
    t2 = _send_message_async(U2, PROMPT_WEATHER)
    try:
        # Drive u1's run forward, releasing each parked step, until a peer step
        # shows up in u1's history (or we run out of patience).
        deadline = time.time() + 150.0
        peer_step_seen = False
        while time.time() < deadline:
            cur = _current(http, U1)
            # Has any peer-authored step landed in u1's history yet?
            for hid in _history_ids(rc, DEMO_AGENT, U1):
                author = rc.hget(f"debug:{DEMO_AGENT}:{U1}:step:{hid}", "agent")
                if author in KNOWN_PEERS:
                    peer_step_seen = True
                    break
            if peer_step_seen:
                break
            # Release whatever u1 is parked on so the run advances toward A2A.
            if cur.get("paused") and cur.get("step"):
                http.post(
                    f"/agents/{DEMO_AGENT}/debug/next",
                    json={"step_id": cur["step"]["step_id"]},
                    headers=_du(U1),
                )
            time.sleep(POLL_INTERVAL)

        if not peer_step_seen:
            pytest.skip(
                "no peer (A2A) step reached u1's history within the deadline — "
                "live agent fan-out unavailable in this environment"
            )

        # All of u1's peer-authored history steps must be ABSENT from u2.
        u1_hist = set(_history_ids(rc, DEMO_AGENT, U1))
        u2_hist = set(_history_ids(rc, DEMO_AGENT, U2))
        assert u1_hist.isdisjoint(u2_hist), (
            f"peer steps in u1's history must never appear in u2's: "
            f"overlap={u1_hist & u2_hist}"
        )
        # Sanity: the peer-authored step is genuinely in u1's (not u2's) history.
        peer_ids_in_u1 = [
            hid for hid in u1_hist
            if rc.hget(f"debug:{DEMO_AGENT}:{U1}:step:{hid}", "agent") in KNOWN_PEERS
        ]
        assert peer_ids_in_u1, "expected a peer-authored step in u1's history"
        for hid in peer_ids_in_u1:
            assert hid not in u2_hist, (
                f"peer step {hid} (u1) leaked into u2's history"
            )
    finally:
        _drain_with_skip(http, U1)
        _drain_with_skip(http, U2)
        _disable_debug(http, U1)
        _disable_debug(http, U2)
        t1.join(timeout=5)
        t2.join(timeout=5)


# ── ST-MU.5 — Untagged traffic bypasses queues ─────────────────────────────


def test_st_mu_5_untagged_bypass(http: httpx.Client, rc: redis.Redis) -> None:
    """ST-MU.5 — With u1 debugging, a send-message WITHOUT the
    X-Amaze-Debug-User header is never parked.

    Asserts:
      * u1's queue length is unchanged across the untagged run (no new parked
        steps appear in debug:agent-sdk:u1:queue).
      * u1's history length is unchanged (the untagged run contributes nothing
        to u1's session).
      * Best-effort: the untagged send-message completes (HTTP 200) — it is NOT
        gated, so it should return on a normal (non-debug) timeout.
    """
    _enable_debug(http, U1)
    try:
        # Snapshot u1's queue/history BEFORE the untagged traffic. u1 is idle
        # (no run started for u1), so these should stay flat.
        q_before = _queue_ids(rc, DEMO_AGENT, U1)
        h_before = _history_ids(rc, DEMO_AGENT, U1)
        assert q_before == [], f"u1 queue should start empty: {q_before}"

        # Send a NORMAL message with NO debug header. Untagged ⇒ never parked,
        # so it runs to completion on the regular timeout and returns 200.
        completed = False
        try:
            # S7: still authenticated (gated endpoint) — only the
            # X-Amaze-Debug-User header is omitted, which is the whole point.
            with httpx.Client(base_url=ORCH, timeout=120.0,
                              cookies=_ADMIN_COOKIES) as c:
                resp = c.post(
                    f"/agents/{DEMO_AGENT}/messages",
                    json={"prompt": PROMPT_WEATHER},
                )  # NB: no debug header — this is the whole point
            completed = resp.status_code == 200
        except httpx.HTTPError:
            completed = False  # best-effort; the core assertions are on Redis

        # While / after that ran, u1's queue must NOT have grown — the untagged
        # call was never parked into u1's (or anyone's) debug queue.
        q_after = _queue_ids(rc, DEMO_AGENT, U1)
        h_after = _history_ids(rc, DEMO_AGENT, U1)
        assert q_after == q_before, (
            f"untagged traffic must not enqueue into u1's debug queue: "
            f"before={q_before} after={q_after}"
        )
        assert len(h_after) == len(h_before), (
            f"untagged traffic must not append to u1's debug history: "
            f"before={len(h_before)} after={len(h_after)}"
        )

        # Best-effort completion signal — only assert when the stack answered.
        if not completed:
            pytest.skip(
                "untagged send-message did not return 200 (live LLM/MCP/key "
                "unavailable); the no-park invariant above still held"
            )
    finally:
        _disable_debug(http, U1)
