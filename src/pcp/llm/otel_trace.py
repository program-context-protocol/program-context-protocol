"""Optional OpenTelemetry tracing for PCP's own LLM calls.

Additive only -- token_ledger.yaml stays the deterministic source of truth
(CLAUDE.md: "Every call logs usage + cost to .pcp/token_ledger.yaml"; this
module doesn't touch that invariant, it just also emits a span). Wire
format is OpenTelemetry's GenAI semantic conventions (still experimental
upstream as of 2026-08, attribute names may shift on spec revisions), not
a vendor-specific client -- so the viewer is a free choice (self-hosted
Langfuse, Jaeger, any OTLP collector), not a lock-in. Prior-art check
2026-08-06: OpenLLMetry/OTel GenAI conventions (Apache 2.0) for the wire
format, Langfuse (MIT, self-hostable) as the recommended viewer -- both
reuse-as-dependency, neither vendored.

Inert unless PCP_OTEL_ENDPOINT is set AND the optional `otel` extra
(`pip install program-context-protocol[otel]`) is installed -- lazy
import so this dependency never becomes load-bearing for the base
install, and any failure here is swallowed rather than raised so a
misconfigured or absent OTel collector never breaks a real LLM call.
"""

import os

_tracer = None
_tracer_init_attempted = False


def _get_tracer():
    global _tracer, _tracer_init_attempted
    if _tracer_init_attempted:
        return _tracer
    _tracer_init_attempted = True
    endpoint = os.environ.get("PCP_OTEL_ENDPOINT")
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider = TracerProvider(resource=Resource.create({"service.name": "pcp"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("pcp")
    except Exception:
        _tracer = None
    return _tracer


def record_span(command: str, model: str | None, session_id: str | None,
                 usage: dict, cost_usd: float | None) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    model = model or "default"
    gen_ai_system = "google" if model.startswith("agy") else "anthropic"
    try:
        with tracer.start_as_current_span(f"pcp.llm.{command}") as span:
            span.set_attribute("gen_ai.system", gen_ai_system)
            span.set_attribute("gen_ai.request.model", model)
            if session_id:
                span.set_attribute("gen_ai.conversation.id", str(session_id))
            span.set_attribute("gen_ai.usage.input_tokens", usage.get("input_tokens", 0))
            span.set_attribute("gen_ai.usage.output_tokens", usage.get("output_tokens", 0))
            if cost_usd is not None:
                span.set_attribute("pcp.cost_usd", cost_usd)
            span.set_attribute("pcp.command", command)
    except Exception:
        pass
