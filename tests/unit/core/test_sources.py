"""Shared source operations (FR-SRC-01, FR-SRC-05, FR-AGT-01)."""

import asyncio
import logging

import pytest

from lakeloom.core.sources import (
    close_sources,
    describe_with_version,
    list_all_tables,
    parse_table_name,
    source_status,
    start_sources,
)
from lakeloom.errors import LakeloomError
from lakeloom.sources.base import LakeSource, SourceCapabilities
from lakeloom.sources.models import TableDescription, TableRef, TableVersion

CAPS = SourceCapabilities(profiling=False, versions=True, constraints=False)


class FakeSource:
    kind = "fake"
    om_service = None
    capabilities = CAPS

    def __init__(self, layout: dict[str | None, dict[str, list[str]]]) -> None:
        self.name = "fake"
        self.layout = layout

    async def list_catalogs(self) -> list[str]:
        return sorted(c for c in self.layout if c is not None)

    async def list_schemas(self, catalog: str | None) -> list[str]:
        if catalog is None and None not in self.layout:
            raise LakeloomError("validation_error", "needs a catalog", "pass one")
        return sorted(self.layout.get(catalog, {}))

    async def list_tables(self, catalog: str | None, schema: str) -> list[TableRef]:
        names = self.layout.get(catalog, {}).get(schema, [])
        return [TableRef(catalog=catalog, schema=schema, name=n) for n in names]

    async def describe_table(self, ref: TableRef) -> TableDescription:
        return TableDescription(ref=ref, columns=[], version=TableVersion())

    async def get_table_version(self, ref: TableRef) -> TableVersion:
        return TableVersion(version=7)

    async def aclose(self) -> None:
        return None


def _src(layout: dict[str | None, dict[str, list[str]]]) -> LakeSource:
    return FakeSource(layout)  # pyright: ignore[reportReturnType]


async def test_list_all_tables_includes_catalog_less_tables() -> None:
    src = _src({"retail": {"sales": ["a", "b"]}, None: {"loose": ["c"]}})

    names = [t.full_name for t in await list_all_tables(src)]

    assert names == ["retail.sales.a", "retail.sales.b", "loose.c"]
    assert [t.name for t in await list_all_tables(src, "retail", "sales")] == ["a", "b"]
    assert await list_all_tables(src, "nope") == []


async def test_list_all_tables_when_catalog_required() -> None:
    src = _src({"main": {"s": ["t"]}})

    assert [t.full_name for t in await list_all_tables(src)] == ["main.s.t"]


async def test_describe_with_version_fills_version() -> None:
    # Unity Catalog's describe leaves the version empty by design.
    src = _src({"retail": {"sales": ["a"]}})

    d = await describe_with_version(src, TableRef(catalog="retail", schema="sales", name="a"))

    assert d.version.version == 7


@pytest.mark.parametrize(
    ("name", "ref"),
    [
        ("c.s.t", TableRef(catalog="c", schema="s", name="t")),
        ("s.t", TableRef(schema="s", name="t")),
    ],
)
def test_parse_table_name(name: str, ref: TableRef) -> None:
    assert parse_table_name(name) == ref


@pytest.mark.parametrize("name", ["t", "a.b.c.d", "a..b", ""])
def test_parse_table_name_rejects(name: str) -> None:
    with pytest.raises(LakeloomError) as info:
        parse_table_name(name)
    assert info.value.code == "validation_error"


class Slow:
    kind = "mcp"
    capabilities = CAPS

    def __init__(self, delay: float, fail_close: bool = False) -> None:
        self.delay = delay
        self.fail_close = fail_close
        self.available = False
        self.last_error: LakeloomError | None = LakeloomError(
            "upstream_unavailable", "not started", "check the logs"
        )
        self.closed = False

    async def start(self) -> None:
        await asyncio.sleep(self.delay)
        self.available, self.last_error = True, None

    async def aclose(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


async def test_start_sources_is_bounded_and_concurrent(caplog: pytest.LogCaptureFixture) -> None:
    fast, hung = Slow(0.01), Slow(10)
    sources: dict[str, LakeSource] = {"fast": fast, "hung": hung}  # pyright: ignore[reportAssignmentType]
    caplog.set_level(logging.INFO, logger="lakeloom.sources")

    await start_sources(sources, bound=0.3)

    assert source_status("fast", sources["fast"]).available is True
    status = source_status("hung", sources["hung"])
    assert status.available is False
    assert status.error is not None
    assert status.error.code == "upstream_unavailable"
    assert status.error.hint == "check the logs"
    assert "timed out" in caplog.text
    assert "source 'hung' unavailable" in caplog.text


async def test_close_sources_survives_failures() -> None:
    a, b = Slow(0, fail_close=True), Slow(0)
    sources: dict[str, LakeSource] = {"a": a, "b": b}  # pyright: ignore[reportAssignmentType]

    await close_sources(sources)

    assert a.closed and b.closed


class NoVersions(FakeSource):
    capabilities = SourceCapabilities(profiling=False, versions=False, constraints=False)

    async def get_table_version(self, ref: TableRef) -> TableVersion:
        raise LakeloomError("validation_error", "no versions", "see list_sources")


async def test_describe_with_version_without_versions_capability() -> None:
    # The OpenMetadata MCP mapping binds no get_table_version: describe must still work.
    src: LakeSource = NoVersions({"r": {"s": ["a"]}})  # pyright: ignore[reportAssignmentType]

    d = await describe_with_version(src, TableRef(catalog="r", schema="s", name="a"))

    assert d.version == TableVersion()
