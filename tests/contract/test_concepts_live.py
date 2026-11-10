"""Concept search and explanation against the seeded retail model in real OM (FR-CTX-03)."""

import pytest
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.concepts import explain_concept, find_concepts
from lakeloom.core.models import ModelService
from lakeloom.om.client import OMClient

pytestmark = pytest.mark.contract

ORDER_IRI = "http://localhost:8000/ontology/retail/Order"


@pytest.fixture
def client(om_url: str, om_bot_token: str) -> OMClient:
    return OMClient(om_url, SecretStr(om_bot_token))


async def test_explain_order_live(client: OMClient) -> None:
    settings = Settings(public_url="http://localhost:8000")  # pyright: ignore[reportArgumentType]
    models = ModelService(client, settings)
    try:
        detail = await explain_concept(models, client, ORDER_IRI)
        assert detail.concept.fqn == "retail.Order"
        assert [c.label for c in detail.children] == ["OrderStatus"]
        assert "Customer" in {r.target.label for r in detail.relations}
        assert "https://schema.org/Order" in {m.target_iri for m in detail.mappings}
        assert "orders" in {a.fqn.rsplit(".", 1)[-1] for a in detail.realizations.items}
        page = await find_concepts(models, client, "client")
        assert page.items[0].concept.label == "Customer"
    finally:
        await client.aclose()
