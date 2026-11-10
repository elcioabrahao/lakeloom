"""OpenTelemetry traces and metrics (NFR-OPS-02) and trace ids in logs (NFR-OPS-03)."""

import json
import logging
from collections.abc import Iterator

import pytest
from kit import AuthKit, DepsFactory
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
    assert any(sp.name == "mcp.tool/get_server_info" for sp in exporter.get_finished_spans())


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
        "lakeloom_scan_duration_seconds_count", {"source": "fixture_delta", "outcome": "completed"}
    )
    assert count and count >= 1


Fixture = tuple[InMemorySpanExporter, InMemoryMetricReader, TelemetryHandle]
DB_URL_TEXT = "postgresql://user:hunter2@db.internal/lakeloom"


def assert_no_exception_events(exporter: InMemorySpanExporter, *leaks: str) -> None:
    finished = exporter.get_finished_spans()
    assert finished
    for span in finished:
        assert not [e for e in span.events if e.name == "exception"], span.name
        blob = json.dumps(
            {
                "name": span.name,
                "attrs": dict(span.attributes or {}),
                "status": span.status.description,
                "events": [e.name for e in span.events],
            },
            default=str,
        )
        for leak in leaks:
            assert leak not in blob, (span.name, leak)


async def test_http_span_never_records_exception_text(spans: Fixture) -> None:
    from starlette.types import Receive, Scope, Send

    from lakeloom.server.http_app import RequestContextMiddleware

    exporter, _, _ = spans

    async def boom(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError(DB_URL_TEXT)

    app = RequestContextMiddleware(boom)
    scope: Scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(_m: dict[str, object]) -> None: ...

    with pytest.raises(RuntimeError):
        await app(scope, receive, send)  # pyright: ignore[reportArgumentType]

    assert_no_exception_events(exporter, "hunter2")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == "RuntimeError"


async def test_scan_span_unknown_source_hides_argument(
    spans: Fixture, make_deps: DepsFactory, kit: AuthKit
) -> None:
    from kit import mcp_session
    from test_tools_sources import RW

    from lakeloom.metrics import REGISTRY

    exporter, _, _ = spans
    async with mcp_session(create_server_app(make_deps()), kit.token(scope=RW)) as session:
        result = await session.call_tool("scan_lake_source", {"source": "ghost-source"})

    assert result.is_error is True
    assert_no_exception_events(exporter, "ghost-source")
    scan = next(s for s in exporter.get_finished_spans() if s.name == "scan")
    assert scan.status.status_code.name == "ERROR"
    assert scan.status.description == "LakeloomError"
    assert scan.attributes is not None
    assert scan.attributes["lakeloom.source"] == "unknown"
    assert scan.attributes["lakeloom.scan.outcome"] == "error"
    # Failed scans are measured too, under a bounded label.
    count = REGISTRY.get_sample_value(
        "lakeloom_scan_duration_seconds_count", {"source": "unknown", "outcome": "error"}
    )
    assert count and count >= 1


async def test_resource_span_never_records_exception_text(
    spans: Fixture, make_deps: DepsFactory, kit: AuthKit
) -> None:
    from kit import mcp_session
    from mcp.shared.exceptions import MCPError

    exporter, _, _ = spans
    async with mcp_session(create_server_app(make_deps()), kit.token()) as session:
        with pytest.raises(MCPError):
            await session.read_resource("lakeloom://models/ghost-model/ontology.zzz")
        with pytest.raises(MCPError):
            await session.read_resource("lakeloom://assets/ghost.fqn.table/context.md")

    assert_no_exception_events(exporter, "ghost-model", "ghost.fqn")
    resource = [s for s in exporter.get_finished_spans() if s.name == "mcp.resource"]
    assert resource
    assert all(s.status.status_code.name == "ERROR" for s in resource)


async def test_tool_span_hides_unexpected_failure(
    spans: Fixture, make_deps: DepsFactory, kit: AuthKit
) -> None:
    from kit import mcp_session, om_down

    exporter, _, _ = spans
    async with mcp_session(create_server_app(make_deps(om_down)), kit.token()) as session:
        await session.call_tool("get_server_info", {})

    assert_no_exception_events(exporter, "connection refused")
    tool = next(s for s in exporter.get_finished_spans() if s.name == "mcp.tool/get_server_info")
    assert tool.attributes is not None
    assert "lakeloom.outcome" in tool.attributes


async def test_om_span_status_only_for_5xx_and_transport_failures(spans: Fixture) -> None:
    import httpx

    from lakeloom.om.client import OMClient, OMError, OMUnavailable

    exporter, _, _ = spans

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(404)
        if request.url.path.endswith("/rdf/status"):
            return httpx.Response(503)
        raise httpx.ConnectError(DB_URL_TEXT, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    om = OMClient("http://om.test", None, http=http)
    with pytest.raises(OMError):
        await om.get_version()
    with pytest.raises(OMUnavailable):
        await om.get_rdf_status()
    with pytest.raises(OMUnavailable):
        await om._request("get_table_by_name", path_args={"fqn": "a.b"})  # pyright: ignore[reportPrivateUsage]
    await http.aclose()

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    notfound = by_name["om.get_version"]
    assert notfound.attributes is not None
    assert notfound.attributes["http.status_code"] == 404
    assert notfound.status.status_code.name == "UNSET"
    assert by_name["om.get_rdf_status"].status.status_code.name == "ERROR"
    assert by_name["om.get_table_by_name"].status.status_code.name == "ERROR"
    assert_no_exception_events(exporter, "hunter2")
