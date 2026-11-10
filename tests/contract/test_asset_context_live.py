"""Asset context for the seeded orders table in real OM (FR-CTX-01, M1 acceptance)."""

import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.context import get_asset_context, render_json, render_markdown
from lakeloom.core.models import ModelService
from lakeloom.om.client import OMClient
from lakeloom.sources.config import DeltaSourceConfig
from lakeloom.sources.delta_storage import DeltaStorageSource

pytestmark = pytest.mark.contract

LAKE = Path(__file__).resolve().parents[1] / "fixtures" / "lake"
ORDERS = "lakeloom_fixture.retail.sales.orders"
COLUMN_CONCEPTS = {
    "order_id": "retail.OrderIdentifier",
    "customer_id": "retail.CustomerIdentifier",
    "status": "retail.Order.OrderStatus",
    "total_amount": "retail.MonetaryAmount",
    "order_date": "retail.OrderDate",
}


@pytest.fixture
def client(om_url: str, om_bot_token: str) -> OMClient:
    return OMClient(om_url, SecretStr(om_bot_token))


async def test_orders_context_live_three_forms(client: OMClient) -> None:
    config = DeltaSourceConfig.model_validate(
        {
            "type": "delta",
            "name": "fixture_delta",
            "om_service": "lakeloom_fixture",
            "tables": [
                {"uri": f"file://{LAKE}/orders", "catalog": "retail", "schema": "sales"},
            ],
        }
    )
    sources = {"fixture_delta": DeltaStorageSource(config)}
    models = ModelService(client, Settings(public_url="http://localhost:8000"))  # pyright: ignore[reportArgumentType]
    try:
        contexts = [
            await get_asset_context(a, models=models, om=client, sources=sources)
            for a in (ORDERS, "retail.sales.orders", f"file://{LAKE}/orders/")
        ]
        assert {c.asset.table_fqn for c in contexts} == {ORDERS}
        for ctx in contexts:
            assert [c.fqn for c in ctx.table_concepts] == ["retail.Order"]
            got = {c.name: [r.fqn for r in c.concepts] for c in ctx.columns}
            assert got == {k: [v] for k, v in COLUMN_CONCEPTS.items()}
            md = render_markdown(ctx)
            for name, concept in COLUMN_CONCEPTS.items():
                assert f"`{name}`" in md and f"`{concept}`" in md
            assert render_json(ctx)["asset"]["table_fqn"] == ORDERS
        started = time.perf_counter()
        await get_asset_context(ORDERS, models=models, om=client, sources=sources)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.5, f"warm latency {elapsed:.3f}s"
    finally:
        await client.aclose()
