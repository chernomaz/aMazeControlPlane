"""
Sprint S4 Phase 6 system tests — `GET /export` (T6.1) — ST-S4.19.

Tests the new ZIP-export endpoint against the live `amaze-platform`
container, in the same style as `tests/test_s4.py`. We do NOT spin up
`tests/compose.test.yml` — the demo stack on host ports is the device
under test.

Override hosts:
  AMAZE_ORCHESTRATOR   http://localhost:8001

Locked T6.1 contract:
  GET /export?agent=&start=&end=&content=traces,audit&format=zip
  → application/zip with Content-Disposition filename
  ZIP contains traces.json (list of full trace dicts)
                + audit.csv (header + rows, columns per CSV_COLUMNS)
  Errors: 400 invalid-content / invalid-format / invalid-range / empty-content
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import zipfile

import httpx
import pytest


ORCH = os.environ.get("AMAZE_ORCHESTRATOR", "http://localhost:8001")

# Live demo agent — already approved + has policy + chat_endpoint.
DEMO_AGENT = "agent-sdk"

# Header set documented by services/orchestrator/export.py CSV_COLUMNS.
EXPECTED_CSV_HEADER = [
    "stream_id",
    "trace_id",
    "span_id",
    "agent_id",
    "session_id",
    "kind",
    "target",
    "tool",
    "ts",
    "denied",
    "denial_reason",
    "indirect",
    "has_tool_calls_input",
    "alert",
    "input",
    "output",
]


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    with httpx.Client(base_url=ORCH, timeout=120.0) as c:
        yield c


# ── helpers ────────────────────────────────────────────────────────────────


def _open_zip(resp: httpx.Response) -> zipfile.ZipFile:
    """Cheap wrapper: assert reasonable response shape, return open ZIP."""
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("content-type", "").startswith("application/zip"), (
        resp.headers
    )
    cd = resp.headers.get("content-disposition", "")
    assert "filename=" in cd, f"Content-Disposition missing filename: {cd!r}"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    return zf


# ───────────────────────────────────────────────────────────────────────────
# Atomic
# ───────────────────────────────────────────────────────────────────────────


def test_export_round_trip(http: httpx.Client) -> None:
    """Defaults: 200, ZIP with traces.json + audit.csv, valid headers."""
    resp = http.get("/export")
    zf = _open_zip(resp)

    names = set(zf.namelist())
    assert "traces.json" in names, names
    assert "audit.csv" in names, names

    # traces.json — non-empty bytes, parses as a JSON list.
    traces_bytes = zf.read("traces.json")
    assert traces_bytes, "traces.json is empty"
    traces = json.loads(traces_bytes.decode("utf-8"))
    assert isinstance(traces, list), f"traces.json must be a JSON list: {type(traces)}"

    # audit.csv — non-empty, header matches the documented columns,
    # parses cleanly with csv.DictReader.
    csv_bytes = zf.read("audit.csv")
    assert csv_bytes, "audit.csv is empty"
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    assert reader.fieldnames == EXPECTED_CSV_HEADER, reader.fieldnames
    # Force read-through; we don't require any rows here (an empty audit
    # window is permitted) — but iteration must not blow up.
    rows = list(reader)
    for row in rows:
        # Every row keys against the same header.
        assert set(row.keys()) == set(EXPECTED_CSV_HEADER), row


def test_export_agent_filter(http: httpx.Client) -> None:
    """`agent=` filters audit.csv rows to just that agent."""
    resp = http.get("/export", params={"agent": DEMO_AGENT})
    zf = _open_zip(resp)

    csv_bytes = zf.read("audit.csv")
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    for row in reader:
        assert row["agent_id"] == DEMO_AGENT, row

    # Edge case: unknown agent → 200 with empty traces.json/empty audit body.
    miss = http.get("/export", params={"agent": "zzz-nonexistent"})
    zf2 = _open_zip(miss)

    traces2 = json.loads(zf2.read("traces.json").decode("utf-8"))
    assert traces2 == [], f"unknown-agent traces.json should be []: {traces2}"

    csv2 = zf2.read("audit.csv").decode("utf-8")
    reader2 = csv.DictReader(io.StringIO(csv2))
    assert reader2.fieldnames == EXPECTED_CSV_HEADER, reader2.fieldnames
    assert list(reader2) == [], "unknown-agent audit.csv should have no rows"


def test_export_content_traces_only(http: httpx.Client) -> None:
    """`content=traces` → ZIP holds only traces.json."""
    resp = http.get("/export", params={"content": "traces"})
    zf = _open_zip(resp)

    names = set(zf.namelist())
    assert "traces.json" in names
    assert "audit.csv" not in names, f"audit.csv must NOT be present: {names}"


def test_export_content_audit_only(http: httpx.Client) -> None:
    """`content=audit` → ZIP holds only audit.csv."""
    resp = http.get("/export", params={"content": "audit"})
    zf = _open_zip(resp)

    names = set(zf.namelist())
    assert "audit.csv" in names
    assert "traces.json" not in names, f"traces.json must NOT be present: {names}"


def test_export_invalid_content(http: httpx.Client) -> None:
    """Unknown content token → 400 with allowed-values hint."""
    resp = http.get("/export", params={"content": "garbage"})
    assert resp.status_code == 400, resp.text
    detail = resp.json().get("detail", "")
    assert "traces" in detail and "audit" in detail, detail


def test_export_invalid_format(http: httpx.Client) -> None:
    """Anything other than format=zip → 400."""
    resp = http.get("/export", params={"format": "tar"})
    assert resp.status_code == 400, resp.text


def test_export_invalid_time_range(http: httpx.Client) -> None:
    """`start > end` → 400."""
    resp = http.get(
        "/export",
        params={"start": 2_000_000_000, "end": 1_000_000_000},
    )
    assert resp.status_code == 400, resp.text


# ───────────────────────────────────────────────────────────────────────────
# Integration / end-to-end — ST-S4.19
# ───────────────────────────────────────────────────────────────────────────


def test_export_round_trip_integration(http: httpx.Client) -> None:
    """ST-S4.19 — Send a message, then export and find that trace_id in both
    traces.json and audit.csv within the past hour."""
    # 1) Send a benign message to ensure ≥1 fresh trace exists.
    msg = http.post(
        f"/agents/{DEMO_AGENT}/messages",
        json={"prompt": "search for current weather in Berlin"},
        timeout=120.0,
    )
    assert msg.status_code == 200, msg.text
    body = msg.json()
    tid = body.get("trace_id")
    if not tid:
        pytest.skip(f"no trace_id from send-message: {body}")

    # 2) Pull the export for the past hour for that agent.
    now = int(time.time())
    resp = http.get(
        "/export",
        params={
            "agent": DEMO_AGENT,
            "start": now - 3600,
            "end": now,
            "content": "traces,audit",
        },
    )
    zf = _open_zip(resp)

    # 3) traces.json — at least one entry, with our trace_id present.
    traces = json.loads(zf.read("traces.json").decode("utf-8"))
    assert isinstance(traces, list) and traces, (
        f"expected ≥1 trace in export, got {traces}"
    )
    trace_ids = {t.get("trace_id") for t in traces}
    assert tid in trace_ids, (
        f"sent trace_id {tid} not in export traces.json ids={trace_ids}"
    )

    # 4) audit.csv — at least one row matching that trace_id.
    csv_bytes = zf.read("audit.csv")
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    matches = [row for row in reader if row.get("trace_id") == tid]
    assert matches, f"no audit.csv row with trace_id={tid}"
    # And the agent_id filter held in the audit rows too.
    for row in matches:
        assert row["agent_id"] == DEMO_AGENT, row
