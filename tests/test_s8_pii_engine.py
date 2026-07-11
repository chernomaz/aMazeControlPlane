"""
Sprint S8 unit tests — PII engine + proxy addon coordination (no docker required).

Covers the addon-level pieces that don't need a live stack:
  * pii_engine.redact / redact_json_text_fields across every canonical entity
  * PiiRedactor.request input redaction + output-rule metadata
  * PiiRedactor.response buffered JSON redaction
  * PiiRedactor POST-SSE stream handler (frame parsing + rewrite + audit)
  * AuditLog GET-SSE frame rewrite via the in-process entities cache
  * AuditLog `responseheaders` short-circuit on `amaze_pii_owned_stream`

The addon chain itself is asserted for order + presence — the request/response
smoke tests for the live stack live in tests/test_s8_pii.py.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.proxy.pii_engine import redact, redact_json_text_fields, PII_NLP_MODE
from services.proxy.pii_redactor import PiiRedactor, _redact_sse_frame
from services.proxy import audit_log
from services.proxy.audit_log import (
    _pii_cache_put, _pii_cache_get, _pii_entities_cache,
    _rewrite_get_sse_frame, _safe_redact_json,
)
from services.proxy.policy import Policy, PiiConfig, ToolPiiConfig, PiiRule


# ---------------------------------------------------------------------------
# pii_engine.redact — one case per canonical entity, plus boundary/no-op
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,entities,expected", [
    ("Ping alice@example.com",                       ["EMAIL_ADDRESS"], "Ping <EMAIL_ADDRESS>"),
    ("Card 4111-1111-1111-1111",                     ["CREDIT_CARD"],   "Card <CREDIT_CARD>"),
    ("Call +14155552019",                            ["PHONE_NUMBER"],  "Call <PHONE_NUMBER>"),
    ("SSN 123-45-6789",                              ["US_SSN"],        "SSN <US_SSN>"),
    ("SSN 123 45 6789",                              ["US_SSN"],        "SSN <US_SSN>"),
    ("SSN 123456789",                                ["US_SSN"],        "SSN <US_SSN>"),
    ("From 192.168.1.10",                            ["IP_ADDRESS"],    "From <IP_ADDRESS>"),
    ("See https://acme.com/x",                       ["URL"],           "See <URL>"),
    ("IBAN GB82WEST12345698765432",                  ["IBAN_CODE"],     "IBAN <IBAN_CODE>"),
    ("Meeting with Jane Smith tomorrow",             ["PERSON"],        "Meeting with <PERSON> tomorrow"),
    ("Office at 500 Market Street, San Francisco, CA 94103",
        ["LOCATION"], "Office at <LOCATION>"),
    ("email me at john.doe@acme.com about Jane Smith",
        ["EMAIL_ADDRESS", "PERSON"],
        "email me at <EMAIL_ADDRESS> about <PERSON>"),
    ("Contact ops at 415-555-2019 or ops@acme.com",
        ["PHONE_NUMBER", "EMAIL_ADDRESS"],
        "Contact ops at <PHONE_NUMBER> or <EMAIL_ADDRESS>"),
    ("",                                             ["EMAIL_ADDRESS"], ""),
    ("no pii here",                                  [],                "no pii here"),
    ("no pii here",                                  ["EMAIL_ADDRESS"], "no pii here"),
    ("Not-SSN 000-11-2222",                          ["US_SSN"],        "Not-SSN 000-11-2222"),
])
def test_redact_matrix(text: str, entities: list[str], expected: str) -> None:
    assert redact(text, entities) == expected


def test_engine_default_mode_regex() -> None:
    assert PII_NLP_MODE == "regex"


def test_redact_json_walk_leaves_non_string_leaves_alone() -> None:
    obj = {
        "content": [{"type": "text", "text": "ops@acme.com and 415-555-2019"}],
        "isError": False,
        "meta": {"count": 2, "nested": [1, 2, {"phone": "call 415-555-2019"}]},
    }
    r = redact_json_text_fields(obj, ["EMAIL_ADDRESS", "PHONE_NUMBER"])
    d = json.dumps(r)
    assert "ops@acme.com" not in d
    assert "415-555-2019" not in d
    assert r["isError"] is False
    assert r["meta"]["count"] == 2
    assert r["meta"]["nested"][:2] == [1, 2]


# ---------------------------------------------------------------------------
# PiiRedactor — request hook
# ---------------------------------------------------------------------------

def _fake_flow(body: dict, tool: str, agent: str = "agent-x") -> MagicMock:
    flow = MagicMock()
    flow.response = None
    flow.metadata = {"amaze_kind": "mcp", "amaze_mcp_tool": tool, "amaze_agent": agent}
    flow.request.content = json.dumps(body).encode()
    flow.request.headers = {}
    flow.request.method = "POST"
    return flow


def test_request_input_redaction_and_content_length() -> None:
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "web_search", "arguments": {
            "query": "email me at john@x.com about Jane Smith",
            "max_results": 5,
        }},
    }
    flow = _fake_flow(body, tool="web_search")
    policy = Policy(
        name="agent-x", allowed_tools=["web_search"],
        pii_config=PiiConfig(
            enabled=True,
            tools={"web_search": ToolPiiConfig(
                input={"query": PiiRule(entities=["EMAIL_ADDRESS", "PERSON"])},
            )},
        ),
    )
    with patch("services.proxy.pii_redactor.policy_store.get_policy",
               new=AsyncMock(return_value=policy)):
        asyncio.run(PiiRedactor().request(flow))

    new_body = json.loads(flow.request.content)
    assert "john@x.com" not in json.dumps(new_body)
    assert "Jane Smith" not in json.dumps(new_body)
    assert new_body["params"]["arguments"]["query"] == (
        "email me at <EMAIL_ADDRESS> about <PERSON>"
    )
    assert new_body["params"]["arguments"]["max_results"] == 5  # int untouched
    assert flow.metadata["amaze_pii_redacted"] is True
    assert flow.request.headers["content-length"] == str(len(flow.request.content))
    assert "amaze_pii_output_entities" not in flow.metadata  # no output rule


def test_request_output_rule_sets_entities_metadata() -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "web_search", "arguments": {}}}
    flow = _fake_flow(body, tool="web_search")
    policy = Policy(
        name="agent-x", allowed_tools=["web_search"],
        pii_config=PiiConfig(
            enabled=True,
            tools={"web_search": ToolPiiConfig(
                input={},
                output=PiiRule(entities=["EMAIL_ADDRESS", "PHONE_NUMBER"]),
            )},
        ),
    )
    with patch("services.proxy.pii_redactor.policy_store.get_policy",
               new=AsyncMock(return_value=policy)):
        asyncio.run(PiiRedactor().request(flow))
    assert flow.metadata["amaze_pii_output_entities"] == ["EMAIL_ADDRESS", "PHONE_NUMBER"]
    # amaze_pii_owned_stream is only set in responseheaders, not request.
    assert "amaze_pii_owned_stream" not in flow.metadata


def test_request_no_pii_config_is_passthrough() -> None:
    body = {"method": "tools/call",
            "params": {"name": "t", "arguments": {"q": "email@x.com"}}}
    flow = _fake_flow(body, tool="t")
    orig = flow.request.content
    with patch("services.proxy.pii_redactor.policy_store.get_policy",
               new=AsyncMock(return_value=Policy(name="agent-x"))):
        asyncio.run(PiiRedactor().request(flow))
    assert flow.request.content == orig
    assert "amaze_pii_redacted" not in flow.metadata


def test_request_bails_for_llm_kind() -> None:
    flow = _fake_flow({"method": "tools/call", "params": {}}, tool="")
    flow.metadata["amaze_kind"] = "llm"
    with patch("services.proxy.pii_redactor.policy_store.get_policy",
               new=AsyncMock()) as gp:
        asyncio.run(PiiRedactor().request(flow))
    gp.assert_not_awaited()  # never even fetched the policy


# ---------------------------------------------------------------------------
# PiiRedactor — response (buffered)
# ---------------------------------------------------------------------------

def test_response_buffered_json_redacts_and_updates_length() -> None:
    pr = PiiRedactor()
    flow = MagicMock()
    flow.metadata = {"amaze_pii_output_entities": ["EMAIL_ADDRESS"]}
    flow.response.headers = {"content-type": "application/json"}
    payload = json.dumps({
        "result": {"content": [{"type": "text", "text": "reach ops@acme.com"}]},
    })
    flow.response.content = payload.encode()
    asyncio.run(pr.response(flow))
    body = json.loads(flow.response.content)
    assert "ops@acme.com" not in json.dumps(body)
    assert "<EMAIL_ADDRESS>" in json.dumps(body)
    assert flow.metadata["amaze_pii_redacted"] is True
    assert flow.response.headers["content-length"] == str(len(flow.response.content))


def test_response_sse_bypassed_when_stream_owned() -> None:
    pr = PiiRedactor()
    flow = MagicMock()
    flow.metadata = {
        "amaze_pii_output_entities": ["EMAIL_ADDRESS"],
        "amaze_pii_owned_stream": True,
    }
    orig = b"streamed body should not be touched by response()"
    flow.response.headers = {"content-type": "text/event-stream"}
    flow.response.content = orig
    asyncio.run(pr.response(flow))
    assert flow.response.content == orig


# ---------------------------------------------------------------------------
# PiiRedactor — SSE frame rewrite
# ---------------------------------------------------------------------------

def test_post_sse_frame_redacted_and_reserialized() -> None:
    frame = (
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"Call 415-555-2019 or ops@acme.com"}]}}'
    )
    rewritten, changed = _redact_sse_frame(frame, ["PHONE_NUMBER", "EMAIL_ADDRESS"])
    assert changed
    assert "415-555-2019" not in rewritten
    assert "ops@acme.com" not in rewritten
    assert "<PHONE_NUMBER>" in rewritten
    assert "<EMAIL_ADDRESS>" in rewritten
    assert "event: message" in rewritten  # non-data lines preserved


def test_post_sse_frame_ignores_protocol_noise() -> None:
    for junk in ("data: ping", "data: [DONE]", "event: heartbeat"):
        rewritten, changed = _redact_sse_frame(junk, ["EMAIL_ADDRESS"])
        assert not changed
        assert rewritten == junk


# ---------------------------------------------------------------------------
# AuditLog — GET-SSE frame rewrite via in-process cache
# ---------------------------------------------------------------------------

def test_get_sse_rewrite_with_cached_entities() -> None:
    _pii_entities_cache.clear()
    _pii_cache_put("sess-A", "42", ["EMAIL_ADDRESS", "PHONE_NUMBER"])
    frame = (
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":42,'
        '"result":{"content":[{"type":"text","text":"Call 415-555-2019 or ops@acme.com"}]}}'
    )
    new_frame, jid, audit_payload = _rewrite_get_sse_frame(frame, "sess-A")
    assert jid == 42
    assert "415-555-2019" not in new_frame
    assert "ops@acme.com" not in new_frame
    assert "<PHONE_NUMBER>" in new_frame
    assert "<EMAIL_ADDRESS>" in new_frame
    assert "event: message" in new_frame
    # audit_payload is the redacted JSON so the audit writer sees redacted output.
    assert "<EMAIL_ADDRESS>" in audit_payload


def test_get_sse_rewrite_without_cache_entry_pass_through() -> None:
    _pii_entities_cache.clear()
    frame = 'data: {"jsonrpc":"2.0","id":99,"result":{"content":[{"type":"text","text":"Hi there"}]}}'
    new_frame, jid, audit_payload = _rewrite_get_sse_frame(frame, "sess-none")
    assert jid == 99
    assert "Hi there" in new_frame
    assert audit_payload  # non-empty — audit still receives raw payload


def test_get_sse_rewrite_ignores_non_result_frames() -> None:
    frame = 'event: ping\ndata: {"jsonrpc":"2.0","method":"notifications/message"}'
    new_frame, jid, audit_payload = _rewrite_get_sse_frame(frame, "sess-x")
    assert jid is None
    assert audit_payload == ""
    assert "event: ping" in new_frame


def test_safe_redact_json_never_raises() -> None:
    # Fix #1/#2: the canonical safe_redact_json (imported by audit_log as
    # `_safe_redact_json`) returns the input SHAPE unchanged on error. Same
    # helper is re-exported by pii_engine and used by pii_redactor — one
    # implementation, one failure semantic, no drift.
    with patch("services.proxy.pii_engine.redact_json_text_fields",
               side_effect=RuntimeError("boom")):
        result = _safe_redact_json({"x": "email@x.com"}, ["EMAIL_ADDRESS"])
    assert result == {"x": "email@x.com"}


def test_pii_cache_ttl_sweeps_expired() -> None:
    # Fix #3: entries older than MCP_PENDING_TTL get swept on next insert.
    import time as _time
    _pii_entities_cache.clear()
    # Insert a stale entry directly (bypass _pii_cache_put's sweep).
    _pii_entities_cache[("sess-old", "1")] = (
        _time.monotonic() - audit_log.MCP_PENDING_TTL - 1, ["EMAIL_ADDRESS"],
    )
    _pii_cache_put("sess-new", "2", ["PERSON"])
    assert ("sess-old", "1") not in _pii_entities_cache
    assert _pii_cache_get("sess-new", "2") == ["PERSON"]


def test_pii_cache_get_returns_none_on_expired() -> None:
    import time as _time
    _pii_entities_cache.clear()
    _pii_entities_cache[("sess-x", "3")] = (
        _time.monotonic() - audit_log.MCP_PENDING_TTL - 1, ["PERSON"],
    )
    assert _pii_cache_get("sess-x", "3") is None
    assert ("sess-x", "3") not in _pii_entities_cache


# ---------------------------------------------------------------------------
# Addon chain — order + presence
# ---------------------------------------------------------------------------

def test_addon_chain_places_pii_redactor_before_audit_log() -> None:
    from services.proxy.main import addons
    names = [a._name for a in addons]
    assert "pii_redactor" in names
    assert "audit_log" in names
    assert names.index("pii_redactor") < names.index("audit_log")
    # And after PolicyEnforcer (so amaze_mcp_tool is set).
    assert names.index("pii_redactor") > names.index("enforcer")


def test_addon_chain_exact_order() -> None:
    from services.proxy.main import addons
    assert [a._name for a in addons] == [
        "session", "tracer", "enforcer", "graph", "debug_pauser",
        "stream_blocker", "pii_redactor", "counters", "audit_log", "router",
    ]
