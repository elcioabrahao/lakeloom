"""Upstream MCP adapter against an in-process fake MCP server (FR-SRC-05, NFR-SEC-14)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.types import CallToolResult, ListToolsResult, Tool, ToolAnnotations

from lakeloom.config import ConfigError, Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.base import ProfilePolicy
from lakeloom.sources.config import McpSourceConfig, load_sources_config
from lakeloom.sources.mcp_adapter import McpUpstreamSource, build_source
from lakeloom.sources.mcp_mapping import McpMapping, parse_mapping
from lakeloom.sources.models import TableRef
from lakeloom.untrusted import UntrustedText

from .fake_upstream import INJECTION, Fake, factory_for

DESCRIBE_FIELDS = {
    "comment": "comment",
    "tags": "tags[].tagFQN",
    "columns": (
        "columns[].{name: name, data_type: type, position: position, comment: doc, "
        "nullable: !required}"
    ),
    "constraints": "constraints[].{kind: type, columns: columns, ref_columns: refs}",
}


def mapping(describe_tool: str = "get_table", list_tool: str = "search_tables") -> McpMapping:
    return parse_mapping(
        {
            "version": 1,
            "operations": {
                "list_tables": {
                    "tool": list_tool,
                    "arguments": {"schema": "{schema}", "cursor": "{cursor}"},
                    "result": {"items": "tables[?kind=='table']", "fields": {"name": "name"}},
                    "pagination": {"cursor": "next"},
                },
                "describe_table": {
                    "tool": describe_tool,
                    "arguments": {"full_name": "{full_name}"},
                    "result": {"fields": DESCRIBE_FIELDS},
                    "not_found": "statusCode == `404`",
                },
            },
        },
        where="test",
    )


def config(**extra: Any) -> McpSourceConfig:
    return McpSourceConfig.model_validate(
        {
            "type": "mcp",
            "name": "fake",
            "transport": "streamable_http",
            "url": "http://upstream.invalid/mcp",
            "mapping": "m.yaml",
            **extra,
        }
    )


ORDERS = TableRef(catalog="retail", schema="sales", name="orders")


@asynccontextmanager
async def started(source: McpUpstreamSource) -> AsyncIterator[McpUpstreamSource]:
    await source.start()
    try:
        yield source
    finally:
        await source.aclose()


async def test_fr_src_05_list_and_describe_via_mapping() -> None:
    fake = Fake()
    source = McpUpstreamSource(config(), mapping(), session_factory=factory_for(fake))
    assert source.available is False  # not started yet
    async with started(source):
        assert source.available is True
        assert source.last_error is None
        assert source.capabilities.constraints is True
        assert source.capabilities.profiling is False

        tables = await source.list_tables("retail", "sales")
        assert [t.name for t in tables] == ["customers", "drop_all", "orders", "products"]
        assert all(t.catalog == "retail" and t.schema_ == "sales" for t in tables)
        pages = [c for c in fake.calls if c[0] == "search_tables"]
        assert [c[1]["cursor"] for c in pages] == [None, "2"]  # followed the cursor

        desc = await source.describe_table(ORDERS)
        assert desc.ref == ORDERS
        assert desc.comment == UntrustedText(text="Customer orders", source="mcp:fake")
        assert desc.tags == ["Tier.Tier1"]
        assert [(c.name, c.data_type, c.position, c.nullable) for c in desc.columns] == [
            ("order_id", "bigint", 1, False),
            ("customer_id", "bigint", 2, True),
            ("status", "string", 3, True),
        ]
        assert [c.kind for c in desc.constraints] == ["primary_key", "foreign_key"]
        fk = desc.constraints[1]
        assert fk.ref_table == TableRef(catalog="retail", schema="sales", name="customers")
        assert fk.ref_columns == ["customer_id"]
        assert ("get_table", {"full_name": "retail.sales.orders"}) in fake.calls

        with pytest.raises(LakeloomError) as info:
            await source.describe_table(TableRef(catalog="retail", schema="sales", name="nope"))
        assert info.value.code == "not_found"

        for call in (
            source.list_catalogs(),
            source.list_schemas("retail"),
            source.get_table_version(ORDERS),
            source.profile_table(
                ORDERS,
                ProfilePolicy(
                    samples=False, sensitive_tags=[], top_k=1, low_cardinality=1, value_max_length=1
                ),
            ),
        ):
            with pytest.raises(LakeloomError) as info:
                await call
            assert info.value.code == "validation_error"
    assert source.available is False


async def test_missing_tool_marks_unavailable() -> None:
    fake = Fake()
    source = McpUpstreamSource(
        config(), mapping(describe_tool="get_tablez"), session_factory=factory_for(fake)
    )
    async with started(source):
        assert source.available is False
        err = source.last_error
        assert err is not None
        assert err.code == "upstream_unavailable"
        assert err.message == "upstream tool 'get_tablez' not found"
        assert err.hint == "update the mapping or the upstream server"
        with pytest.raises(LakeloomError) as info:
            await source.list_tables("retail", "sales")
        assert info.value is err
    assert fake.calls == []  # nothing was called on a half-matching server


async def test_server_down_marks_unavailable() -> None:
    @asynccontextmanager
    async def down(_c: McpSourceConfig, _t: str | None) -> AsyncIterator[ClientSession]:
        raise ConnectionRefusedError("nope")
        yield  # pragma: no cover

    source = McpUpstreamSource(config(), mapping(), session_factory=down)
    await source.start()
    assert source.available is False
    assert source.last_error is not None
    assert source.last_error.code == "upstream_unavailable"
    assert "fake" in source.last_error.message
    with pytest.raises(LakeloomError):
        await source.describe_table(ORDERS)
    await source.aclose()


async def test_start_times_out() -> None:
    @asynccontextmanager
    async def hang(_c: McpSourceConfig, _t: str | None) -> AsyncIterator[ClientSession]:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")
        yield  # pragma: no cover

    source = McpUpstreamSource(config(), mapping(), session_factory=hang, connect_timeout=0.2)
    await asyncio.wait_for(source.start(), timeout=5)
    assert source.available is False
    assert source.last_error is not None
    assert "timed out" in source.last_error.message


async def test_start_times_out_on_real_transport_black_hole() -> None:
    """A server that accepts TCP but never answers: start() gives up, the loop survives."""

    async def silent(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)
        w.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        source = McpUpstreamSource(
            config(url=f"http://127.0.0.1:{port}/mcp"), mapping(), connect_timeout=0.5
        )
        await asyncio.wait_for(source.start(), timeout=5)
        assert source.available is False
        assert source.last_error is not None
        assert "timed out" in source.last_error.message
        await source.aclose()
    finally:
        server.close()


@pytest.mark.parametrize(
    ("tool", "message"),
    [
        ("broken_shape", "describe_table"),
        ("not_json", "upstream returned non-JSON for describe_table"),
        ("deep", "upstream returned non-JSON for describe_table"),
        ("huge", "too large"),
        ("infinity_text", "upstream returned non-JSON for describe_table"),
        ("nan_text", "upstream returned non-JSON for describe_table"),
    ],
)
async def test_shape_mismatch_upstream_unavailable(tool: str, message: str) -> None:
    source = McpUpstreamSource(
        config(), mapping(describe_tool=tool), session_factory=factory_for(Fake())
    )
    async with started(source):
        assert source.available is True
        with pytest.raises(LakeloomError) as info:
            await source.describe_table(ORDERS)
        assert info.value.code == "upstream_unavailable"
        assert message in info.value.message
        if tool == "broken_shape":
            assert "columns[]" in info.value.message  # names the failing JMESPath


async def test_nfr_sec_14_upstream_text_untrusted() -> None:
    fake = Fake()
    source = McpUpstreamSource(config(), mapping(), session_factory=factory_for(fake))
    async with started(source):
        customers = await source.describe_table(
            TableRef(catalog="retail", schema="sales", name="customers")
        )
        assert customers.comment == UntrustedText(text=INJECTION, source="mcp:fake")
        orders = await source.describe_table(ORDERS)
        status = orders.columns[2]
        assert status.comment == UntrustedText(text=INJECTION, source="mcp:fake")
        # A table named like a tool is listed as data; the adapter never calls it.
        names = [t.name for t in await source.list_tables("retail", "sales")]
        assert "drop_all" in names
        await source.describe_table(TableRef(catalog="retail", schema="sales", name="drop_all"))
    assert {name for name, _ in fake.calls} == {"search_tables", "get_table"}


async def test_fr_src_05_bearer_token_from_env_or_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens: list[str | None] = []
    env_name = "UPSTREAM_TOKEN"
    monkeypatch.setenv(env_name, "from-env")
    source = McpUpstreamSource(
        config(token_env=env_name), mapping(), session_factory=factory_for(Fake(), tokens)
    )
    async with started(source):
        assert source.available
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n", encoding="utf-8")
    source = McpUpstreamSource(
        config(token_file=str(token_file)), mapping(), session_factory=factory_for(Fake(), tokens)
    )
    async with started(source):
        assert source.available
    assert tokens == ["from-env", "from-file"]

    missing = McpUpstreamSource(
        config(token_file=str(tmp_path / "absent")),
        mapping(),
        session_factory=factory_for(Fake(), tokens),
    )
    await missing.start()
    assert missing.available is False
    assert missing.last_error is not None
    assert "from-file" not in missing.last_error.message


def test_fr_src_05_config_validation_and_mapping_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="token_env"):
        names = {"token_env": "A", "token_file": "/x"}
        config(**names)
    with pytest.raises(ValueError, match="url"):
        McpSourceConfig.model_validate(
            {"type": "mcp", "name": "x", "transport": "streamable_http", "mapping": "m.yaml"}
        )
    with pytest.raises(ValueError, match="command"):
        McpSourceConfig.model_validate(
            {"type": "mcp", "name": "x", "transport": "stdio", "mapping": "m.yaml"}
        )

    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "om.yaml").write_text(
        "version: 1\noperations:\n  describe_table:\n    tool: t\n"
        "    arguments: {fqn: '{full_name}'}\n    result: {fields: {columns: columns}}\n",
        encoding="utf-8",
    )
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n  - type: mcp\n    name: om\n    transport: streamable_http\n"
        "    url: http://om:8585/mcp\n    mapping: maps/om.yaml\n",
        encoding="utf-8",
    )
    cfg = load_sources_config(sources).sources[0]
    assert isinstance(cfg, McpSourceConfig)
    assert cfg.mapping == tmp_path / "maps" / "om.yaml"
    built = build_source(cfg, Settings())
    assert isinstance(built, McpUpstreamSource)
    assert built.available is False

    bad = cfg.model_copy(update={"mapping": tmp_path / "absent.yaml"})
    with pytest.raises(ConfigError):
        build_source(bad, Settings())


async def test_jmespath_runtime_error_is_upstream_unavailable() -> None:
    odd = parse_mapping(
        {
            "version": 1,
            "operations": {
                "describe_table": {
                    "tool": "get_table",
                    "arguments": {"full_name": "{full_name}"},
                    "result": {"fields": {"columns": "length(no_such_field)"}},
                }
            },
        },
        where="test",
    )
    source = McpUpstreamSource(config(), odd, session_factory=factory_for(Fake()))
    async with started(source):
        with pytest.raises(LakeloomError) as info:
            await source.describe_table(ORDERS)
    assert info.value.code == "upstream_unavailable"
    assert "length(no_such_field)" in info.value.message


def _single(op: str, spec: dict[str, Any]) -> McpMapping:
    return parse_mapping({"version": 1, "operations": {op: spec}}, where="test")


class _StubSession:
    """Hands the adapter already-decoded structuredContent with non-finite floats, as a
    real HTTP upstream can (pydantic's JSON parser accepts Infinity/NaN); the SDK's
    in-memory transport would turn them into null first."""

    def __init__(self, tool: str, body: dict[str, Any]) -> None:
        self.tool, self.body = tool, body

    async def initialize(self) -> None:
        return None

    async def list_tools(self, *, params: Any = None) -> ListToolsResult:
        return ListToolsResult(tools=[Tool(name=self.tool, input_schema={"type": "object"})])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        return CallToolResult(content=[], structured_content=self.body)


def _stub_factory(tool: str, body: dict[str, Any]) -> Any:
    @asynccontextmanager
    async def factory(_c: McpSourceConfig, _t: str | None) -> AsyncIterator[Any]:
        yield _StubSession(tool, body)

    return factory


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
async def test_non_finite_structured_numbers_are_upstream_unavailable(bad: float) -> None:
    describe = _single(
        "describe_table",
        {
            "tool": "t",
            "arguments": {"full_name": "{full_name}"},
            "result": {"fields": {"columns": DESCRIBE_FIELDS["columns"]}},
        },
    )
    body = {"columns": [{"name": "a", "type": "int", "position": bad}]}
    source = McpUpstreamSource(config(), describe, session_factory=_stub_factory("t", body))
    async with started(source):
        with pytest.raises(LakeloomError) as info:
            await source.describe_table(ORDERS)
    assert info.value.code == "upstream_unavailable"
    assert "columns[0].position" in info.value.message

    profile = _single(
        "profile_table",
        {
            "tool": "t",
            "arguments": {"full_name": "{full_name}"},
            "result": {"fields": {"row_count": "rows", "columns": "columns"}},
        },
    )
    policy = ProfilePolicy(
        samples=False, sensitive_tags=[], top_k=1, low_cardinality=1, value_max_length=1
    )
    for body in (
        {"rows": 3, "columns": [{"name": "a", "null_rate": bad}]},
        {"rows": bad, "columns": []},
    ):
        source = McpUpstreamSource(config(), profile, session_factory=_stub_factory("t", body))
        async with started(source):
            with pytest.raises(LakeloomError) as info:
                await source.profile_table(ORDERS, policy)
        assert info.value.code == "upstream_unavailable"


def _paged(tool: str, **arguments: Any) -> McpMapping:
    return _single(
        "describe_table",
        {
            "tool": tool,
            "arguments": {
                "full_name": "{full_name}",
                "column_offset": "{column_offset}",
                **arguments,
            },
            "result": {
                "fields": {
                    "comment": "comment",
                    "columns": DESCRIBE_FIELDS["columns"],
                    "columns_truncated": "truncated",
                }
            },
            "column_pagination": {"more": "more"},
        },
    )


async def test_fr_src_05_describe_follows_column_pages() -> None:
    fake = Fake()
    source = McpUpstreamSource(
        config(), _paged("get_table_paged"), session_factory=factory_for(fake)
    )
    async with started(source):
        desc = await source.describe_table(ORDERS)
    assert [c.name for c in desc.columns] == ["order_id", "customer_id", "status"]
    assert "lakeloom.columns_truncated" not in desc.properties
    offsets = [c[1]["offset"] for c in fake.calls if c[0] == "get_table_paged"]
    assert offsets == [None, 1, 2]  # first page omits the offset; then it is an integer


async def test_fr_src_05_partial_column_page_is_flagged_truncated() -> None:
    # The upstream claims more columns but returns an empty page: keep what we have, flag it.
    source = McpUpstreamSource(
        config(), _paged("get_table_paged", empty_after=2), session_factory=factory_for(Fake())
    )
    async with started(source):
        desc = await source.describe_table(ORDERS)
    assert [c.name for c in desc.columns] == ["order_id", "customer_id"]
    assert desc.properties["lakeloom.columns_truncated"] == "true"

    # A mapping without column paging still surfaces the upstream's own truncation flag.
    partial = _single(
        "describe_table",
        {
            "tool": "get_table_partial",
            "arguments": {"full_name": "{full_name}"},
            "result": {
                "fields": {"columns": DESCRIBE_FIELDS["columns"], "columns_truncated": "truncated"}
            },
        },
    )
    source = McpUpstreamSource(config(), partial, session_factory=factory_for(Fake()))
    async with started(source):
        desc = await source.describe_table(ORDERS)
    assert len(desc.columns) == 1
    assert desc.properties["lakeloom.columns_truncated"] == "true"


async def test_fr_src_05_warns_when_mapped_tools_are_not_read_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="lakeloom.sources.mcp")
    source = McpUpstreamSource(config(), mapping(), session_factory=factory_for(Fake()))
    async with started(source):
        assert source.available is True  # warn only
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "not annotated read-only" in message
    assert "get_table, search_tables" in message


class _AnnotatedSession(_StubSession):
    async def list_tools(self, *, params: Any = None) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name=self.tool,
                    input_schema={"type": "object"},
                    annotations=ToolAnnotations(read_only_hint=True),
                )
            ]
        )


async def test_fr_src_05_no_warning_for_read_only_mapped_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="lakeloom.sources.mcp")

    @asynccontextmanager
    async def factory(_c: McpSourceConfig, _t: str | None) -> AsyncIterator[Any]:
        yield _AnnotatedSession("t", {})

    describe = _single(
        "describe_table", {"tool": "t", "arguments": {}, "result": {"fields": {"columns": "c"}}}
    )
    source = McpUpstreamSource(config(), describe, session_factory=factory)
    async with started(source):
        assert source.available is True
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
