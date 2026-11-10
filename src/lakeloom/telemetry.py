"""OpenTelemetry traces and metrics (NFR-OPS-02), off unless an OTLP endpoint is configured.

``configure_telemetry`` builds a private ``TracerProvider`` and ``MeterProvider`` (never the
process-global ones, so repeated configuration in tests cannot clash) and keeps them in a
module-level handle. Without ``LAKELOOM_OTEL_EXPORTER_OTLP_ENDPOINT`` (and no test hooks)
nothing is installed: spans come from the global no-op tracer and the recorders do nothing.

Span attributes carry only names, outcomes and counts: never tokens, headers, SQL or URLs,
tool arguments or untrusted text (NFR-SEC-11, NFR-SEC-14).
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from lakeloom import __version__
from lakeloom.config import Settings

__all__ = [
    "TelemetryHandle",
    "configure_telemetry",
    "record_om_request",
    "record_resource_read",
    "record_scan",
    "record_tool_call",
    "tracer",
]

_log = logging.getLogger("lakeloom.telemetry")
_lock = threading.Lock()
_active: "TelemetryHandle | None" = None


@dataclass
class TelemetryHandle:
    """What ``configure_telemetry`` installed. ``enabled`` is False when it is all no-op."""

    enabled: bool
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None
    _instruments: dict[str, Any] = field(default_factory=dict[str, Any], repr=False)
    _closed: bool = field(default=False, repr=False)

    def shutdown(self) -> None:
        """Flush and stop the exporters; safe to call twice."""
        global _active
        with _lock:
            if self._closed:
                return
            self._closed = True
            if _active is self:
                _active = None
        for provider in (self.tracer_provider, self.meter_provider):
            if provider is None:
                continue
            try:
                provider.force_flush()
                provider.shutdown()
            except Exception as exc:  # exporting must never break shutdown
                _log.warning("telemetry shutdown failed (%s)", type(exc).__name__)


def is_active() -> bool:
    return _active is not None


def configure_telemetry(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> TelemetryHandle:
    """Install tracing and metrics. Idempotent: an active handle is returned as is.

    ``span_exporter`` and ``metric_reader`` are test hooks that replace the OTLP exporters.
    """
    global _active
    with _lock:
        if _active is not None:
            return _active
        endpoint = settings.otel_exporter_otlp_endpoint
        if not endpoint and span_exporter is None and metric_reader is None:
            return TelemetryHandle(enabled=False)
        resource = Resource.create({"service.name": "lakeloom", "service.version": __version__})
        base = (endpoint or "").rstrip("/")

        tracer_provider = TracerProvider(resource=resource)
        if span_exporter is not None:
            tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        elif endpoint:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
            )
        reader = metric_reader
        if reader is None and endpoint:
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{base}/v1/metrics")
            )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[reader] if reader is not None else []
        )
        meter = meter_provider.get_meter("lakeloom")
        handle = TelemetryHandle(
            enabled=True,
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            _instruments={
                "tool_calls": meter.create_counter(
                    "lakeloom.tool_calls", description="MCP tool calls."
                ),
                "resource_reads": meter.create_counter(
                    "lakeloom.resource_reads", description="MCP resource reads."
                ),
                "om_duration": meter.create_histogram(
                    "lakeloom.om.request.duration",
                    unit="s",
                    description="OpenMetadata request latency.",
                ),
                "scan_duration": meter.create_histogram(
                    "lakeloom.scan.duration", unit="s", description="Lake source scan duration."
                ),
            },
        )
        _active = handle
        return handle


def tracer() -> trace.Tracer:
    """The Lakeloom tracer: the configured provider's, else the global (no-op) one."""
    handle = _active
    if handle is not None and handle.tracer_provider is not None:
        return handle.tracer_provider.get_tracer("lakeloom")
    return trace.get_tracer("lakeloom")


def _counter(name: str) -> Counter | None:
    handle = _active
    return None if handle is None else handle._instruments.get(name)  # pyright: ignore[reportPrivateUsage]


def _histogram(name: str) -> Histogram | None:
    handle = _active
    return None if handle is None else handle._instruments.get(name)  # pyright: ignore[reportPrivateUsage]


def record_tool_call(tool: str, outcome: str) -> None:
    if (c := _counter("tool_calls")) is not None:
        c.add(1, {"tool": tool, "outcome": outcome})


def record_resource_read(resource: str, outcome: str) -> None:
    if (c := _counter("resource_reads")) is not None:
        c.add(1, {"resource": resource, "outcome": outcome})


def record_om_request(operation: str, status: str, seconds: float) -> None:
    if (h := _histogram("om_duration")) is not None:
        h.record(seconds, {"operation": operation, "status": status})


def record_scan(source: str, seconds: float) -> None:
    if (h := _histogram("scan_duration")) is not None:
        h.record(seconds, {"source": source})
