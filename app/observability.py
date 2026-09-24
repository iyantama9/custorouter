"""Private, fail-safe OpenTelemetry setup.

The exporter receives only route/model/provider/status metadata. It does not
capture authorization headers, prompt messages, or upstream credentials.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_tracing(app: FastAPI):
    """Instrument FastAPI + HTTPX and return a provider to shut down later."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({
        SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", "llm-router"),
        SERVICE_VERSION: os.getenv("APP_VERSION", "unknown"),
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    # Health and Prometheus scrape traffic would otherwise dominate traces.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    HTTPXClientInstrumentor().instrument()
    return provider


def annotate_current_request(*, model: str | None, provider: str | None,
                             streamed: bool | None = None) -> None:
    span = trace.get_current_span()
    if not span or not span.is_recording():
        return
    if model:
        span.set_attribute("llm.request.model", str(model)[:200])
    if provider:
        span.set_attribute("llm.provider", str(provider)[:40])
    if streamed is not None:
        span.set_attribute("llm.stream", bool(streamed))
