"""Upstream MCP adapter against OpenMetadata's own MCP server (FR-SRC-05).

Uses the shipped ``examples/mcp-mappings/openmetadata.yaml`` against the dev OM, whose
fixture service ``lakeloom_fixture`` is seeded by ``om-seed`` (Task 5).
"""

from pathlib import Path

import httpx
import pytest
from stack import OM_URL, REPO

from lakeloom.config import Settings
from lakeloom.sources.config import McpSourceConfig
from lakeloom.sources.mcp_adapter import build_source
from lakeloom.sources.models import TableRef
from lakeloom.untrusted import UntrustedText

pytestmark = pytest.mark.contract

FIXTURE_TABLES = ["customers", "invoices", "orders", "products"]


async def test_fr_src_05_om_mcp_mapping_lists_fixture_tables(
    om_bot_token: str, tmp_path: Path
) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            (await http.get(f"{OM_URL}/api/v1/system/version")).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"OpenMetadata not reachable at {OM_URL}: {exc!r}")
    token_file = tmp_path / "om_bot_token"
    token_file.write_text(om_bot_token, encoding="utf-8")
    config = McpSourceConfig(
        type="mcp",
        name="om",
        transport="streamable_http",
        url=f"{OM_URL}/mcp",  # pyright: ignore[reportArgumentType]
        token_file=token_file,
        mapping=REPO / "examples/mcp-mappings/openmetadata.yaml",
        om_service="lakeloom_fixture",
    )
    source = build_source(config, Settings())
    await source.start()
    try:
        assert source.available, source.last_error and source.last_error.message
        assert "retail" in await source.list_catalogs()
        assert "sales" in await source.list_schemas("retail")

        tables = await source.list_tables("retail", "sales")
        assert sorted(t.name for t in tables) == FIXTURE_TABLES
        assert all(t.catalog == "retail" and t.schema_ == "sales" for t in tables)

        orders = await source.describe_table(
            TableRef(catalog="retail", schema="sales", name="orders")
        )
        assert [c.name for c in orders.columns] == [
            "order_id",
            "customer_id",
            "order_date",
            "status",
            "total_amount",
        ]
        assert orders.columns[0].data_type == "bigint"
        assert isinstance(orders.comment, UntrustedText)
        assert orders.comment.source == "mcp:om"
        assert {c.kind for c in orders.constraints} == {"primary_key", "foreign_key"}
        fk = next(c for c in orders.constraints if c.kind == "foreign_key")
        assert fk.ref_table == TableRef(catalog="retail", schema="sales", name="customers")

        with pytest.raises(Exception) as info:
            await source.describe_table(TableRef(catalog="retail", schema="sales", name="nope"))
        assert getattr(info.value, "code", None) == "not_found"
    finally:
        await source.aclose()
