"""Prometheus metrics (NFR-OPS-02). Kept apart from ``server/`` so ``om/`` can import them.

A dedicated registry avoids duplicate-registration errors when apps are built repeatedly
(tests) and keeps default process collectors out of the exposition.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "lakeloom_http_requests_total",
    "HTTP requests handled.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
TOOL_CALLS = Counter(
    "lakeloom_tool_calls_total",
    "MCP tool calls.",
    ["tool", "outcome"],
    registry=REGISTRY,
)
RESOURCE_READS = Counter(
    "lakeloom_resource_reads_total",
    "MCP resource reads.",
    ["resource", "outcome"],
    registry=REGISTRY,
)
OM_REQUEST_SECONDS = Histogram(
    "lakeloom_om_request_seconds",
    "OpenMetadata request latency by operation.",
    ["operation"],
    registry=REGISTRY,
)
SCAN_SECONDS = Histogram(
    "lakeloom_scan_duration_seconds",
    "Lake source scan duration by outcome (completed, partial, failed, error).",
    ["source", "outcome"],
    registry=REGISTRY,
)
