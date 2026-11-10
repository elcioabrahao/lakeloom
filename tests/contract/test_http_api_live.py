"""The /v1 publication API against the live stack (FR-PUB-02, FR-PUB-07)."""

from collections.abc import Iterator
from urllib.parse import quote

import httpx
import pytest
from rdflib import Graph

pytestmark = pytest.mark.contract

ORDERS = "lakeloom_fixture.retail.sales.orders"
ORDER_IRI = "http://localhost:8000/ontology/retail/Order"
COLUMN_CONCEPTS = {
    "order_id": "retail.OrderIdentifier",
    "customer_id": "retail.CustomerIdentifier",
    "status": "retail.Order.OrderStatus",
    "total_amount": "retail.MonetaryAmount",
    "order_date": "retail.OrderDate",
}


@pytest.fixture
def api(lakeloom_url: str, agent_token: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=lakeloom_url, headers={"Authorization": f"Bearer {agent_token}"}, timeout=60
    ) as client:
        yield client


def test_http_models_turtle_parses_and_etag_stable(api: httpx.Client) -> None:
    first = api.get("/v1/models/retail", headers={"Accept": "text/turtle"})
    second = api.get("/v1/models/retail", headers={"Accept": "text/turtle"})

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/turtle")
    assert len(Graph().parse(data=first.text, format="turtle")) > 0
    etag = first.headers["etag"]
    assert etag == second.headers["etag"]
    assert first.headers["last-modified"] and 'rel="via"' in first.headers["link"]
    cached = api.get("/v1/models/retail", headers={"If-None-Match": etag})
    assert cached.status_code == 304 and cached.content == b""
    assert cached.headers["etag"] == etag


def test_http_orders_context_markdown_lists_column_concepts(api: httpx.Client) -> None:
    r = api.get(f"/v1/assets/{ORDERS}/context", headers={"Accept": "text/markdown"})

    assert r.status_code == 200, r.text
    for name, concept in COLUMN_CONCEPTS.items():
        assert f"`{name}`" in r.text and f"`{concept}`" in r.text


def test_http_concept_order_json(api: httpx.Client) -> None:
    r = api.get(f"/v1/concepts/{quote(ORDER_IRI, safe='')}", headers={"Accept": "application/json"})

    assert r.status_code == 200, r.text
    assert r.json()["concept"]["fqn"] == "retail.Order"
    assert r.headers["etag"].startswith('"sha256:')
