"""M1 acceptance: "scan returns all tables with versions" over the real MCP endpoint.

Runs against the compose dev stack, whose lakeloom service mounts
``deploy/compose/lakeloom-sources.yaml`` (fixture_delta, fixture_uc, openmetadata_mcp).
The ``lakeloom-agent`` client has ``lakeloom:read`` and ``lakeloom:propose`` by default.
"""

from typing import Any

import pytest
from stack import mcp_session, token_scopes

pytestmark = pytest.mark.contract

FIXTURE_TABLES = {"customers", "invoices", "orders", "products"}


async def _call(url: str, token: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with mcp_session(url, token) as session:
        result = await session.call_tool(tool, args)
    assert result.is_error is not True, result
    return dict(result.structured_content or {})


@pytest.mark.parametrize("source", ["fixture_delta", "fixture_uc"])
async def test_m1_acceptance_scan_returns_all_tables_with_versions(
    lakeloom_url: str, agent_token: str, source: str
) -> None:
    assert "lakeloom:propose" in token_scopes(agent_token)

    data = await _call(lakeloom_url, agent_token, "scan_lake_source", {"source": source})

    assert data["status"] == "completed", data["summary"]
    tables = data["record"]["tables"]
    assert {t["ref"]["name"] for t in tables} == FIXTURE_TABLES
    assert all(t["error"] is None for t in tables)
    assert all(t["version"]["version"] is not None for t in tables)
    versions = {t["ref"]["name"]: t["version"]["version"] for t in tables}
    assert versions["orders"] == 1
    assert all(t["ref"]["catalog"] == "retail" and t["ref"]["schema"] == "sales" for t in tables)
    # om-seed registered the fixture tables, so every one resolves in OpenMetadata.
    assert all(t["om_fqn"] for t in tables), data["record"]["warnings"]
    assert data["record"]["principal_type"] == "agent"
    assert data["scan_id"] in data["summary"]


async def test_list_sources_reports_three_available(lakeloom_url: str, agent_token: str) -> None:
    data = await _call(lakeloom_url, agent_token, "list_sources", {})

    by_name = {s["name"]: s for s in data["sources"]}
    assert set(by_name) == {"fixture_delta", "fixture_uc", "openmetadata_mcp"}
    assert {n: s["kind"] for n, s in by_name.items()} == {
        "fixture_delta": "delta",
        "fixture_uc": "unity_catalog",
        "openmetadata_mcp": "mcp",
    }
    for name, status in by_name.items():
        assert status["available"] is True, (name, status["error"])


async def test_describe_table_has_version_for_every_source(
    lakeloom_url: str, agent_token: str
) -> None:
    for source in ("fixture_delta", "fixture_uc"):
        data = await _call(
            lakeloom_url,
            agent_token,
            "describe_table",
            {"source": source, "table": "retail.sales.orders"},
        )
        assert data["version"]["version"] == 1, source
        assert "order_id" in {c["name"] for c in data["columns"]}


async def test_list_tables_over_upstream_mcp(lakeloom_url: str, agent_token: str) -> None:
    data = await _call(
        lakeloom_url, agent_token, "list_tables", {"source": "openmetadata_mcp", "limit": 200}
    )

    assert {t["name"] for t in data["items"]} == FIXTURE_TABLES


async def test_scan_and_describe_over_upstream_mcp(lakeloom_url: str, agent_token: str) -> None:
    # The OpenMetadata mapping binds no get_table_version: tables scan without a version.
    data = await _call(
        lakeloom_url, agent_token, "scan_lake_source", {"source": "openmetadata_mcp"}
    )
    assert data["status"] == "completed", data["summary"]
    assert {t["ref"]["name"] for t in data["record"]["tables"]} == FIXTURE_TABLES

    described = await _call(
        lakeloom_url,
        agent_token,
        "describe_table",
        {"source": "openmetadata_mcp", "table": "retail.sales.orders"},
    )
    assert "order_id" in {c["name"] for c in described["columns"]}
