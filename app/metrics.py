"""Low-cardinality Prometheus metrics for router operations.

Metrics deliberately exclude keys, prompts, request IDs, and client IPs. A
model name is configuration-controlled and bounded by the provider catalog;
status is reduced to a class so Prometheus cannot be exhausted by labels.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest


_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

INFERENCE_REQUESTS = Counter(
    "router_inference_requests_total",
    "Completed inference requests recorded by the router.",
    ("provider", "model", "status_class"),
)
INFERENCE_LATENCY = Histogram(
    "router_inference_latency_seconds",
    "End-to-end upstream request duration recorded at completion.",
    ("provider", "model", "status_class"),
    buckets=_LATENCY_BUCKETS,
)
INFERENCE_TTFT = Histogram(
    "router_inference_ttft_seconds",
    "Time to first streamed response byte.",
    ("provider", "model", "status_class"),
    buckets=_LATENCY_BUCKETS,
)
KEY_ROTATIONS = Counter(
    "router_key_rotations_total",
    "Inference completions that used a rotated upstream key.",
    ("provider", "model"),
)
ACTIVE_INFERENCE = Gauge(
    "router_active_inference_requests",
    "Inference responses currently open, including active streams.",
)
DB_POOL_SIZE = Gauge("router_db_pool_connections", "Asyncpg connections by state.", ("state",))
DB_ACQUIRE = Histogram(
    "router_db_pool_acquire_seconds",
    "Time waiting to acquire an asyncpg connection.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1, 5),
)


def _status_class(status_code: int) -> str:
    try:
        return f"{int(status_code) // 100}xx"
    except (TypeError, ValueError):
        return "unknown"


def _labels(provider: str | None, model: str | None) -> tuple[str, str]:
    return (str(provider or "unknown")[:40], str(model or "unknown")[:200])


def observe_inference(model: str, provider: str | None, status_code: int,
                      latency_ms: int, rotated: bool) -> None:
    labels = (*_labels(provider, model), _status_class(status_code))
    INFERENCE_REQUESTS.labels(*labels).inc()
    INFERENCE_LATENCY.labels(*labels).observe(max(0, int(latency_ms)) / 1000)
    if rotated:
        KEY_ROTATIONS.labels(*_labels(provider, model)).inc()


def observe_ttft(model: str | None, provider: str | None, status_code: int,
                 latency_seconds: float) -> None:
    INFERENCE_TTFT.labels(
        *_labels(provider, model), _status_class(status_code),
    ).observe(max(0.0, latency_seconds))


def observe_active_inference(active: int) -> None:
    ACTIVE_INFERENCE.set(max(0, active))


def observe_db_pool(acquire_seconds: float, size: int, idle: int) -> None:
    DB_ACQUIRE.observe(max(0.0, acquire_seconds))
    DB_POOL_SIZE.labels("in_use").set(max(0, size - idle))
    DB_POOL_SIZE.labels("idle").set(max(0, idle))
    DB_POOL_SIZE.labels("total").set(max(0, size))


def render_metrics() -> bytes:
    return generate_latest()
