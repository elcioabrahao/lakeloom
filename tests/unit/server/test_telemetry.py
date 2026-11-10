"""OpenTelemetry traces and metrics (NFR-OPS-02) and trace ids in logs (NFR-OPS-03)."""

import json
import logging
from collections.abc import Iterator

import pytest
from kit import AuthKit, DepsFactory
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lakeloom.config import Settings
from lakeloom.logging import configure_logging
from lakeloom.server.mcp_app import create_server_app
from lakeloom.telemetry import TelemetryHandle, configure_telemetry, is_active, tracer


@pytest.fixture
def spans() -> Iterator[tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle]]:
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    handle = configure_telemetry(Settings(), span_exporter=exporter, metric_reader=reader)
    yield exporter, reader, handle
    handle.shutdown()


def test_nfr_ops_02_off_by_default() -> None:
    handle = configure_telemetry(Settings())

    assert handle.enabled is False
    assert handle.tracer_provider is None
    assert not is_active()
    with tracer().start_as_current_span("x") as span:
        assert not span.get_span_context().is_valid
    handle.shutdown()
    handle.shutdown()


def test_configure_is_idempotent_and_shutdown_twice(
    spans: tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle],
) -> None:
    _, _, handle = spans
    assert handle.enabled
    assert configure_telemetry(Settings()) is handle
    handle.shutdown()
    handle.shutdown()
    assert not is_active()


def test_otlp_endpoint_enables_without_hooks() -> None:
    handle = configure_telemetry(Settings(otel_exporter_otlp_endpoint="http://127.0.0.1:1/"))
    try:
        assert handle.enabled
    finally:
        handle.shutdown()


async def test_spans_for_tool_and_om_calls(
    spans: tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle],
    make_deps: DepsFactory,
    kit: AuthKit,
) -> None:
    from test_mcp_app import call_server_info

    exporter, reader, _ = spans
    app = create_server_app(make_deps())
    token = kit.token()

    await call_server_info(app, token)

    finished = exporter.get_finished_spans()
    tool = next(s for s in finished if s.name == "mcp.tool/get_server_info")
    assert tool.attributes is not None
    assert tool.attributes["lakeloom.tool"] == "get_server_info"
    assert tool.attributes["lakeloom.outcome"] == "ok"
    assert tool.attributes["lakeloom.principal_type"] in {"agent", "human"}
    assert tool.context is not None
    tool_id = tool.context.span_id
    children = [s for s in finished if s.parent is not None and s.parent.span_id == tool_id]
    assert any(s.name.startswith("om.") for s in children)
    assert any(s.name.startswith("HTTP POST") for s in finished)
    data = reader.get_metrics_data()
    assert data is not None
    names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert {"lakeloom.tool_calls", "lakeloom.om.request.duration"} <= names


async def test_health_endpoints_are_not_traced(
    spans: tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle],
    make_deps: DepsFactory,
) -> None:
    import httpx

    exporter, _, _ = spans
    app = create_server_app(make_deps())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/healthz")
        await c.get("/metrics")
        await c.get("/v1/models")  # 401, but traced

    names = [s.name for s in exporter.get_finished_spans()]
    assert not any("healthz" in n or "metrics" in n for n in names)
    assert any(n.startswith("HTTP GET") for n in names)


def test_log_record_has_trace_id_inside_span(
    spans: tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    with tracer().start_as_current_span("s") as span:
        logging.getLogger("lakeloom.test").info("inside")
        ctx = span.get_span_context()
    logging.getLogger("lakeloom.test").info("outside")

    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    inside = next(r for r in lines if r["message"] == "inside")
    outside = next(r for r in lines if r["message"] == "outside")
    assert inside["trace_id"] == format(ctx.trace_id, "032x")
    assert inside["span_id"] == format(ctx.span_id, "016x")
    assert "trace_id" not in outside


async def test_no_token_in_span_attributes(
    spans: tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle],
    make_deps: DepsFactory,
    kit: AuthKit,
) -> None:
    from test_mcp_app import call_server_info

    exporter, _, _ = spans
    token = kit.token()
    await call_server_info(create_server_app(make_deps()), token)

    def dump(span: ReadableSpan) -> str:
        return json.dumps({"n": span.name, "a": dict(span.attributes or {})}, default=str)

    blob = "\n".join(dump(s) for s in exporter.get_finished_spans())
    assert blob
    assert token not in blob
    assert "Bearer" not in blob and "authorization" not in blob.lower()
    assert "om.test" not in blob  # no URLs
    assert trace.get_current_span() is not None


async def test_scan_span_and_duration_metrics(
    spans: tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle],
    make_deps: DepsFactory,
    kit: AuthKit,
) -> None:
    from kit import mcp_session
    from test_tools_sources import RW, delta_source

    from lakeloom.metrics import REGISTRY
    from lakeloom.state.db import upgrade

    exporter, reader, _ = spans
    deps = make_deps(sources={"fixture_delta": delta_source()})
    await upgrade(deps.engine)
    async with mcp_session(create_server_app(deps), kit.token(scope=RW)) as session:
        await session.call_tool("scan_lake_source", {"source": "fixture_delta"})

    scan = next(s for s in exporter.get_finished_spans() if s.name == "scan")
    assert scan.attributes is not None
    assert scan.attributes["lakeloom.source"] == "fixture_delta"
    assert scan.attributes["lakeloom.scan.tables"] == 4
    assert scan.attributes["lakeloom.scan.status"] == "completed"
    data = reader.get_metrics_data()
    assert data is not None
    names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert "lakeloom.scan.duration" in names
    count = REGISTRY.get_sample_value(
        "lakeloom_scan_duration_seconds_count", {"source": "fixture_delta"}
    )
    assert count and count >= 1
