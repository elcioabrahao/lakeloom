"""MCP resources against the live stack: same bytes as the HTTP API (FR-PUB-01, FR-PUB-02)."""

import httpx
import pytest
from rdflib import Graph
from stack import mcp_session

pytestmark = pytest.mark.contract

ORDERS = "lakeloom_fixture.retail.sales.orders"
COLUMN_CONCEPTS = (
    "retail.OrderIdentifier",
    "retail.CustomerIdentifier",
    "retail.Order.OrderStatus",
    "retail.MonetaryAmount",
    "retail.OrderDate",
)


async def test_ontology_resource_equals_http_turtle(lakeloom_url: str, agent_token: str) -> None:
    async with mcp_session(lakeloom_url, agent_token) as session:
        listed = {str(r.uri) for r in (await session.list_resources()).resources}
        content = (await session.read_resource("lakeloom://models/retail/ontology.ttl")).contents[0]
    assert "lakeloom://models" in listed
    assert "lakeloom://models/retail/ontology.ttl" in listed
    assert content.mime_type == "text/turtle"
    text = getattr(content, "text", "")
    assert len(Graph().parse(data=text, format="turtle")) > 0
    async with httpx.AsyncClient(timeout=60) as client:
        http = await client.get(
            f"{lakeloom_url}/v1/models/retail",
            headers={"Authorization": f"Bearer {agent_token}", "Accept": "text/turtle"},
        )
    assert http.status_code == 200
    assert text.encode() == http.content


async def test_asset_context_resource_lists_column_concepts(
    lakeloom_url: str, agent_token: str
) -> None:
    async with mcp_session(lakeloom_url, agent_token) as session:
        content = (await session.read_resource(f"lakeloom://assets/{ORDERS}/context.md")).contents[
            0
        ]
    assert content.mime_type == "text/markdown"
    text = getattr(content, "text", "")
    for concept in COLUMN_CONCEPTS:
        assert concept in text
