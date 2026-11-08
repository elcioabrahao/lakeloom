"""Unity Catalog connector against the real UC OSS in the dev stack (FR-SRC-02)."""

import os

import httpx
import pytest
from stack import REPO

from lakeloom.sources.config import UnityCatalogSourceConfig
from lakeloom.sources.models import TableRef
from lakeloom.sources.unity_catalog import UnityCatalogSource

pytestmark = pytest.mark.contract

UC_URL = os.environ.get("LAKELOOM_TEST_UC_URL", "http://localhost:8081").rstrip("/")


@pytest.fixture(scope="module")
def uc_url() -> str:
    try:
        httpx.get(f"{UC_URL}/api/2.1/unity-catalog/catalogs", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Unity Catalog not reachable at {UC_URL}: {exc!r}")
    return UC_URL


def _source(url: str, **extra: object) -> UnityCatalogSource:
    config = UnityCatalogSourceConfig.model_validate(
        {"type": "unity_catalog", "name": "uc", "url": url, "catalogs": ["retail"], **extra}
    )
    return UnityCatalogSource(config)


async def test_fr_src_02_uc_oss_lists_fixture_tables(uc_url: str) -> None:
    source = _source(uc_url)
    try:
        assert await source.list_catalogs() == ["retail"]
        assert await source.list_schemas("retail") == ["sales"]
        tables = await source.list_tables("retail", "sales")
        assert [t.name for t in tables] == ["customers", "invoices", "orders", "products"]
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
        assert orders.columns[4].data_type == "decimal(12,2)"
        assert orders.partition_columns == ["status"]
        assert orders.storage_location == "file:///lake/orders"
        assert orders.comment is not None and orders.comment.untrusted
        assert orders.tags == ["Domain.Sales"]
        assert {c.kind for c in orders.constraints} == {"primary_key", "foreign_key"}
    finally:
        await source.aclose()


async def test_fr_src_02_uc_version_via_storage(uc_url: str) -> None:
    # /lake exists only inside the containers; map it onto the host fixture lake.
    source = _source(
        uc_url,
        read_storage=True,
        storage_root_map={"file:///lake": str(REPO / "tests/fixtures/lake")},
    )
    try:
        ref = TableRef(catalog="retail", schema="sales", name="orders")
        assert (await source.get_table_version(ref)).version == 1
    finally:
        await source.aclose()
