"""Unity Catalog connector against recorded UC OSS v0.7.0 responses (FR-SRC-02)."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.base import ProfilePolicy
from lakeloom.sources.config import DeltaSourceConfig, SourcesConfig, UnityCatalogSourceConfig
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.sources.models import TableRef
from lakeloom.sources.registry import build_sources
from lakeloom.sources.unity_catalog import UnityCatalogSource

RECORDED = Path(__file__).parents[2] / "fixtures" / "uc"
LAKE = Path(__file__).parents[2] / "fixtures" / "lake"
BASE = "http://uc.test:8080"
API = f"{BASE}/api/2.1/unity-catalog"


def _json(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def _config(**kwargs: Any) -> UnityCatalogSourceConfig:
    return UnityCatalogSourceConfig.model_validate(
        {"type": "unity_catalog", "name": "uc", "url": BASE, **kwargs}
    )


def _ref(name: str = "orders") -> TableRef:
    return TableRef(catalog="retail", schema="sales", name=name)


def _policy() -> ProfilePolicy:
    return ProfilePolicy(
        samples=False, sensitive_tags=["PII.*"], top_k=5, low_cardinality=50, value_max_length=64
    )


async def test_fr_src_02_list_and_describe(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/catalogs").respond(json=_json("catalogs.json"))
    respx_mock.get(f"{API}/schemas", params={"catalog_name": "retail"}).respond(
        json=_json("schemas.json")
    )
    respx_mock.get(
        f"{API}/tables", params={"catalog_name": "retail", "schema_name": "sales"}
    ).respond(json=_json("tables.json"))
    respx_mock.get(f"{API}/tables/retail.sales.orders").respond(json=_json("table_orders.json"))
    source = UnityCatalogSource(_config(catalogs=["retail"]))
    assert source.kind == "unity_catalog"
    assert not source.capabilities.profiling and not source.capabilities.versions
    assert await source.list_catalogs() == ["retail"]  # "unity" filtered by config.catalogs
    assert await source.list_schemas("retail") == ["sales"]
    tables = await source.list_tables("retail", "sales")
    assert [t.full_name for t in tables] == [
        "retail.sales.customers",
        "retail.sales.invoices",
        "retail.sales.orders",
        "retail.sales.products",
    ]
    desc = await source.describe_table(_ref())
    assert desc.comment is not None
    assert desc.comment.text == "Customer orders, one row per order"
    assert desc.comment.source == "unity_catalog:uc"
    assert desc.comment.untrusted is True
    assert [(c.name, c.data_type, c.nullable, c.position) for c in desc.columns] == [
        ("order_id", "bigint", False, 0),
        ("customer_id", "bigint", True, 1),
        ("order_date", "date", True, 2),
        ("status", "string", True, 3),
        ("total_amount", "decimal(12,2)", True, 4),
    ]
    assert desc.columns[0].comment is not None
    assert desc.columns[0].comment.source == "unity_catalog:uc"
    assert desc.partition_columns == ["status"]
    assert desc.tags == ["Domain.Sales"]
    assert desc.owners == ["data-platform@example.com"]
    assert desc.storage_location == "file:///lake/orders"
    assert desc.properties == {}  # lakeloom.* conventions are consumed, not echoed
    assert [(c.kind, c.columns) for c in desc.constraints] == [
        ("primary_key", ["order_id"]),
        ("foreign_key", ["customer_id"]),
    ]
    fk = desc.constraints[1]
    assert fk.ref_table is not None
    assert (fk.ref_table.catalog, fk.ref_table.schema_, fk.ref_table.name) == (
        "retail",
        "sales",
        "customers",
    )
    assert fk.ref_columns == ["customer_id"]
    assert desc.version.version is None


async def test_describe_maps_databricks_constraints_and_owner(
    respx_mock: respx.MockRouter,
) -> None:
    body = _json("table_orders.json")
    body["properties"] = {}
    body["owner"] = "alice@example.com"
    body["columns"][1]["comment"] = None
    body["table_constraints"] = [
        {"primary_key_constraint": {"name": "pk", "child_columns": ["order_id"]}},
        {
            "foreign_key_constraint": {
                "name": "fk",
                "child_columns": ["customer_id"],
                "parent_table": "retail.sales.customers",
                "parent_columns": ["customer_id"],
            }
        },
    ]
    respx_mock.get(f"{API}/tables/retail.sales.orders").respond(json=body)
    desc = await UnityCatalogSource(_config()).describe_table(_ref())
    assert desc.owners == ["alice@example.com"]
    assert [(c.kind, c.columns) for c in desc.constraints] == [
        ("primary_key", ["order_id"]),
        ("foreign_key", ["customer_id"]),
    ]
    assert desc.constraints[1].ref_table is not None
    assert desc.constraints[1].ref_table.full_name == "retail.sales.customers"
    assert desc.columns[1].comment is None


async def test_pagination_next_page_token(respx_mock: respx.MockRouter) -> None:
    pages = {
        None: {"catalogs": [{"name": "a"}], "next_page_token": "t1"},
        "t1": {"catalogs": [{"name": "b"}], "next_page_token": "t2"},
        "t2": {"catalogs": [{"name": "c"}], "next_page_token": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("page_token")])

    route = respx_mock.get(f"{API}/catalogs").mock(side_effect=handler)
    assert await UnityCatalogSource(_config()).list_catalogs() == ["a", "b", "c"]
    assert route.call_count == 3


async def test_pagination_token_loop_is_upstream_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/catalogs").respond(
        json={"catalogs": [{"name": "a"}], "next_page_token": "same"}
    )
    with pytest.raises(LakeloomError) as err:
        await UnityCatalogSource(_config()).list_catalogs()
    assert err.value.code == "upstream_unavailable"


async def test_token_from_env_not_logged(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("UC_TOKEN_TEST", "s3cr3t-token-value")
    route = respx_mock.get(f"{API}/catalogs").respond(json=_json("catalogs.json"))
    source = UnityCatalogSource(_config(token_env="UC_TOKEN_TEST"))  # noqa: S106
    with caplog.at_level(logging.DEBUG):
        await source.list_catalogs()
    assert route.calls.last.request.headers["authorization"] == "Bearer s3cr3t-token-value"
    assert "s3cr3t-token-value" not in caplog.text
    assert "s3cr3t-token-value" not in repr(source) + repr(vars(source))
    respx_mock.get(f"{API}/schemas").respond(401)
    with pytest.raises(LakeloomError) as err:
        await source.list_schemas("retail")
    assert "s3cr3t-token-value" not in err.value.message + err.value.hint


async def test_no_token_env_sends_no_authorization(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UC_TOKEN_UNSET", raising=False)
    route = respx_mock.get(f"{API}/catalogs").respond(json=_json("catalogs.json"))
    source = UnityCatalogSource(_config(token_env="UC_TOKEN_UNSET"))  # noqa: S106
    await source.list_catalogs()
    assert "authorization" not in route.calls.last.request.headers


async def test_404_not_found(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/tables/retail.sales.zzz").respond(
        404, json=_json("error_table_not_found.json")
    )
    with pytest.raises(LakeloomError) as err:
        await UnityCatalogSource(_config()).describe_table(_ref("zzz"))
    assert err.value.code == "not_found"
    assert err.value.hint


async def test_401_forbidden(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/catalogs").respond(401, json={"message": "no"})
    with pytest.raises(LakeloomError) as err:
        source = UnityCatalogSource(_config(token_env="UC_TOKEN"))  # noqa: S106
        await source.list_catalogs()
    assert err.value.code == "forbidden"
    assert "UC_TOKEN" in err.value.hint


async def test_403_forbidden(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{API}/catalogs").respond(403)
    with pytest.raises(LakeloomError) as err:
        await UnityCatalogSource(_config()).list_catalogs()
    assert err.value.code == "forbidden"


async def test_5xx_and_connect_errors_are_upstream_unavailable(
    respx_mock: respx.MockRouter,
) -> None:
    source = UnityCatalogSource(_config())
    respx_mock.get(f"{API}/catalogs").respond(503)
    with pytest.raises(LakeloomError) as err:
        await source.list_catalogs()
    assert err.value.code == "upstream_unavailable"
    respx_mock.get(f"{API}/catalogs").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LakeloomError) as err2:
        await source.list_catalogs()
    assert err2.value.code == "upstream_unavailable"
    respx_mock.get(f"{API}/catalogs").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LakeloomError) as err3:
        await source.list_catalogs()
    assert err3.value.code == "upstream_unavailable"


async def test_table_name_is_percent_encoded(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(f"{API}/tables/retail.sales.we%2Fird").respond(
        json=_json("table_orders.json")
    )
    await UnityCatalogSource(_config()).describe_table(
        TableRef(catalog="retail", schema="sales", name="we/ird")
    )
    assert route.called


async def test_catalog_required(respx_mock: respx.MockRouter) -> None:
    source = UnityCatalogSource(_config())
    with pytest.raises(LakeloomError) as err:
        await source.list_schemas(None)
    assert err.value.code == "validation_error"


async def test_without_read_storage_no_version_and_no_profile(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(f"{API}/tables/retail.sales.orders").respond(json=_json("table_orders.json"))
    source = UnityCatalogSource(_config())
    version = await source.get_table_version(_ref())
    assert (version.version, version.committed_at) == (None, None)
    with pytest.raises(LakeloomError) as err:
        await source.profile_table(_ref(), _policy())
    assert err.value.code == "validation_error"


async def test_read_storage_delegates_to_delta(respx_mock: respx.MockRouter) -> None:
    body = _json("table_orders.json")
    body["storage_location"] = "file:///lake/orders"
    respx_mock.get(f"{API}/tables/retail.sales.orders").respond(json=body)
    seen: list[DeltaSourceConfig] = []

    def factory(config: DeltaSourceConfig) -> DeltaStorageSource:
        seen.append(config)
        return DeltaStorageSource(config)

    config = _config(read_storage=True, storage_root_map={"file:///lake": str(LAKE)})
    source = UnityCatalogSource(config, delta_factory=factory)
    assert source.capabilities.profiling and source.capabilities.versions
    version = await source.get_table_version(_ref())
    assert version.version == 1
    profile = await source.profile_table(_ref(), _policy())
    assert profile.row_count > 0
    assert profile.ref == _ref()
    assert seen[0].tables[0].uri == f"{LAKE}/orders"


async def test_read_storage_unsupported_scheme_has_no_version(
    respx_mock: respx.MockRouter,
) -> None:
    body = _json("table_orders.json")
    body["storage_location"] = "dbfs:/user/hive/orders"
    respx_mock.get(f"{API}/tables/retail.sales.orders").respond(json=body)
    source = UnityCatalogSource(_config(read_storage=True))
    version = await source.get_table_version(_ref())
    assert (version.version, version.committed_at) == (None, None)
    with pytest.raises(LakeloomError) as err:
        await source.profile_table(_ref(), _policy())
    assert err.value.code == "validation_error"


async def test_read_storage_non_delta_format_has_no_version(
    respx_mock: respx.MockRouter,
) -> None:
    body = _json("table_orders.json")
    body["data_source_format"] = "PARQUET"
    respx_mock.get(f"{API}/tables/retail.sales.orders").respond(json=body)
    version = await UnityCatalogSource(_config(read_storage=True)).get_table_version(_ref())
    assert version.version is None


async def test_registry_builds_unity_catalog() -> None:
    config = SourcesConfig(sources=[_config(om_service="lakeloom_fixture")])
    sources = build_sources(config, Settings())
    source = sources["uc"]
    assert source.kind == "unity_catalog"
    assert source.om_service == "lakeloom_fixture"
    await source.aclose()


async def test_aclose_keeps_injected_client() -> None:
    async with httpx.AsyncClient() as http:
        source = UnityCatalogSource(_config(), http=http)
        await source.aclose()
        assert not http.is_closed
