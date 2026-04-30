import logging
import os

import redis.asyncio as redis
from mitmproxy import http
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import StatusCode

from services.proxy._redis import client as redis_client

log = logging.getLogger(__name__)

_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
_provider = TracerProvider()
_exporter = OTLPSpanExporter(endpoint=_OTLP_ENDPOINT, insecure=True)
_provider.add_span_processor(BatchSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)
_tracer = trace.get_tracer("amaze.proxy")
# Logged on first request (basicConfig hasn't run at module import time).
_startup_logged = False


def _span_name(kind: str, flow: http.HTTPFlow) -> str:
    if kind == "llm":
        return f"llm.{flow.metadata.get('amaze_llm_provider', 'llm')}"
    if kind == "mcp":
        tool = flow.metadata.get("amaze_mcp_tool") or "request"
        server = flow.metadata.get("amaze_mcp_server", flow.request.pretty_host)
        return f"mcp.{server}.{tool}"
    if kind == "a2a":
        return f"a2a.{flow.metadata.get('amaze_target', flow.request.pretty_host)}"
    return f"proxy.{flow.request.pretty_host}"


class Tracer:
    async def request(self, flow: http.HTTPFlow) -> None:
        global _startup_logged
        if not _startup_logged:
            log.info("tracer: OTLP exporter → %s (insecure=True)", _OTLP_ENDPOINT)
            _startup_logged = True
        if flow.metadata.get("amaze_bypass"):
            return

        kind = flow.metadata.get("amaze_kind", "unknown")
        sid = flow.metadata.get("amaze_session")

        # Look up stored parent trace context for this session.
        # If present → this conversation already has a trace_id; reuse it so
        # every call in the session shares the same trace_id (single trace
        # per conversation). If absent → this is the first call; we'll start
        # a new span and store ITS traceparent below.
        parent_ctx = None
        had_stored = False
        if sid:
            try:
                r = await redis_client()
                stored = await r.get(f"trace_context:{sid}")
                if stored:
                    parent_ctx = extract({"traceparent": stored})
                    had_stored = True
            except redis.RedisError as e:
                log.warning("tracer: redis error fetching trace context: %s", e)

        span = _tracer.start_span(_span_name(kind, flow), context=parent_ctx)
        span.set_attribute("amaze.agent_id", flow.metadata.get("amaze_agent", "unknown"))
        span.set_attribute("amaze.host", flow.request.pretty_host)
        span.set_attribute("http.method", flow.request.method)
        flow.metadata["otel_span"] = span

        # Inject W3C traceparent into the forwarded request.
        carrier: dict = {}
        inject(carrier, context=trace.set_span_in_context(span))
        if "traceparent" in carrier:
            flow.request.headers["traceparent"] = carrier["traceparent"]

        # Persist this span's traceparent so:
        #   (a) subsequent calls in the same session inherit it as parent
        #       (single trace_id per conversation), and
        #   (b) if the destination host is itself a registered agent, we
        #       propagate trace context to the target's session BEFORE the
        #       request is forwarded — the target's outbound calls (which
        #       happen synchronously while we're still in this request) will
        #       then inherit the parent context and share the trace_id.
        #
        # Both writes use NX (set-if-not-exists). For (a) this guards two
        # concurrent first calls. For (b) this is critical — without NX, a
        # second conversation propagating to the same target agent would
        # overwrite the first one's trace_id, silently merging unrelated
        # conversations into one trace. NX ensures the first conversation
        # to reach an agent "owns" that agent's trace context until its
        # session expires (24h TTL).
        flow.metadata["_tracer_carrier"] = carrier
        if "traceparent" in carrier:
            tp = carrier["traceparent"]
            try:
                r = await redis_client()
                if sid and not had_stored:
                    await r.set(f"trace_context:{sid}", tp, ex=86400, nx=True)
                # A2A cross-agent propagation: only propagate if the
                # destination host is actually a registered agent (Redis is
                # the source of truth — we don't rely on policy classification
                # which runs after Tracer in the chain).
                target_host = flow.request.pretty_host
                target_sid = await r.get(f"agent_session:{target_host}")
                if target_sid:
                    # NX: don't clobber an in-flight conversation already
                    # using this target. The receiving SDK should also
                    # propagate via the `traceparent` header we injected
                    # above; this Redis path is a fallback.
                    await r.set(f"trace_context:{target_sid}", tp, ex=86400, nx=True)
            except redis.RedisError as e:
                log.warning("tracer: redis error in trace context propagation: %s", e)

    async def response(self, flow: http.HTTPFlow) -> None:
        span = flow.metadata.get("otel_span")
        if span is None:
            return

        kind = flow.metadata.get("amaze_kind", "unknown")
        span.set_attribute("amaze.kind", kind)
        if flow.metadata.get("amaze_mcp_tool"):
            span.set_attribute("amaze.tool", flow.metadata["amaze_mcp_tool"])
        if flow.metadata.get("amaze_target"):
            span.set_attribute("amaze.target", flow.metadata["amaze_target"])
        if flow.metadata.get("amaze_llm_provider"):
            span.set_attribute("amaze.llm_provider", flow.metadata["amaze_llm_provider"])

        if flow.response is not None and flow.response.status_code >= 400:
            span.set_attribute("amaze.denied", True)
            span.set_status(StatusCode.ERROR)
        if flow.metadata.get("amaze_budget_alert") or flow.metadata.get("amaze_rate_alert"):
            span.set_attribute("amaze.alert", True)
        if flow.metadata.get("amaze_violation"):
            span.set_attribute("amaze.violation", True)

        span.end()
