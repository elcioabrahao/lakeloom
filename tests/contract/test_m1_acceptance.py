"""M1 acceptance (REQUIREMENTS section 13) re-asserted through MCP tools, MCP resources and HTTP."""

import hashlib
from urllib.parse import quote

import httpx
import pytest
from rdflib import Graph
from stack import mcp_session

pytestmark = pytest.mark.contract

SOURCE = "fixture_delta"
TABLES = {"orders", "customers", "invoices", "products"}
ORDERS = "lakeloom_fixture.retail.sales.orders"
TABLE_CONCEPT = "retail.Order"
COLUMN_CONCEPTS = {
    "order_id": "retail.OrderIdentifier",
    "customer_id": "retail.CustomerIdentifier",
    "status": "retail.Order.OrderStatus",
    "total_amount": "retail.MonetaryAmount",
    "order_date": "retail.OrderDate",
}
# tool format -> (HTTP Accept, resource extension, rdflib format)
RDF_FORMATS = {
    "turtle": ("text/turtle", "ttl", "turtle"),
    "jsonld": ("application/ld+json", "jsonld", "json-ld"),
    "rdfxml": ("application/rdf+xml", "owl", "xml"),
    "ntriples": ("application/n-triples", "nt", "nt"),
}


def _assert_lists_concepts(markdown: str) -> None:
    assert f"`{TABLE_CONCEPT}`" in markdown
    for name, concept in COLUMN_CONCEPTS.items():
        assert f"`{name}`" in markdown and f"`{concept}`" in markdown


async def test_scan_returns_all_tables_with_versions(lakeloom_url: str, agent_token: str) -> None:
    async with mcp_session(lakeloom_url, agent_token) as session:
        result = await session.call_tool("scan_lake_source", {"source": SOURCE})
    assert result.is_error is not True, result
    data = dict(result.structured_content or {})
    assert data["status"] == "completed", data["summary"]
    tables = data["record"]["tables"]
    assert {t["ref"]["name"] for t in tables} == TABLES
    assert all(t["error"] is None and t["version"]["version"] is not None for t in tables)


async def test_orders_context_lists_column_concepts_on_all_surfaces(
    lakeloom_url: str, agent_token: str
) -> None:
    async with mcp_session(lakeloom_url, agent_token) as session:
        tool = await session.call_tool("get_asset_context", {"asset": ORDERS})
        assert tool.is_error is not True, tool
        block = tool.content[0]
        assert block.type == "text"
        resource = (await session.read_resource(f"lakeloom://assets/{ORDERS}/context.md")).contents[
            0
        ]
    async with httpx.AsyncClient(
        base_url=lakeloom_url, headers={"Authorization": f"Bearer {agent_token}"}, timeout=60
    ) as client:
        http = await client.get(
            f"/v1/assets/{quote(ORDERS, safe='')}/context", headers={"Accept": "text/markdown"}
        )
    assert http.status_code == 200

    _assert_lists_concepts(str(block.text))
    _assert_lists_concepts(getattr(resource, "text", ""))
    _assert_lists_concepts(http.text)
    assert getattr(resource, "text", "").encode() == http.content


@pytest.mark.parametrize("fmt", list(RDF_FORMATS))
async def test_rdf_exports_identical_across_tool_http_and_resource(
    lakeloom_url: str, agent_token: str, fmt: str
) -> None:
    accept, ext, rdflib_format = RDF_FORMATS[fmt]
    async with mcp_session(lakeloom_url, agent_token) as session:
        tool = await session.call_tool("export_model", {"model": "retail", "format": fmt})
        assert tool.is_error is not True, tool
        block = tool.content[0]
        assert block.type == "text"
        tool_text = str(block.text)
        resource = (
            await session.read_resource(f"lakeloom://models/retail/ontology.{ext}")
        ).contents[0]
    async with httpx.AsyncClient(
        base_url=lakeloom_url, headers={"Authorization": f"Bearer {agent_token}"}, timeout=60
    ) as client:
        http = await client.get("/v1/models/retail", headers={"Accept": accept})
    assert http.status_code == 200

    resource_text = getattr(resource, "text", "")
    assert tool_text.encode() == http.content == resource_text.encode(), fmt
    assert dict(tool.structured_content or {})["sha256"] == hashlib.sha256(http.content).hexdigest()
    assert len(Graph().parse(data=tool_text, format=rdflib_format)) > 0


async def test_skos_export_parses_as_turtle(lakeloom_url: str, agent_token: str) -> None:
    async with mcp_session(lakeloom_url, agent_token) as session:
        tool = await session.call_tool("export_model", {"model": "retail", "format": "skos"})
    assert tool.is_error is not True, tool
    block = tool.content[0]
    assert block.type == "text"
    assert len(Graph().parse(data=str(block.text), format="turtle")) > 0
