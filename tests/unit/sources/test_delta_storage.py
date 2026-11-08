from datetime import datetime
from pathlib import Path

import pytest

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.config import DeltaSourceConfig, DeltaTableEntry, SourcesConfig
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.sources.models import TableRef
from lakeloom.sources.registry import build_sources
from lakeloom.untrusted import UntrustedText

FIXTURE = Path(__file__).parents[2] / "fixtures" / "lake"


def _config(root: Path = FIXTURE, names: tuple[str, ...] | None = None) -> DeltaSourceConfig:
    tables = names or ("customers", "products", "orders", "invoices")
    return DeltaSourceConfig(
        type="delta",
        name="fixture",
        tables=[
            DeltaTableEntry(uri=(root / n).as_uri(), catalog="retail", schema="sales")
            for n in tables
        ],
    )


def _ref(name: str) -> TableRef:
    return TableRef(catalog="retail", schema="sales", name=name)


async def test_fr_src_03_lists_four_tables() -> None:
    source = DeltaStorageSource(_config())
    assert source.kind == "delta"
    assert source.capabilities.versions and source.capabilities.constraints
    assert await source.list_catalogs() == ["retail"]
    assert await source.list_schemas("retail") == ["sales"]
    tables = await source.list_tables("retail", "sales")
    assert [t.name for t in tables] == ["customers", "invoices", "orders", "products"]
    assert await source.list_tables("retail", "nope") == []


async def test_fr_src_03_describe_orders() -> None:
    desc = await DeltaStorageSource(_config()).describe_table(_ref("orders"))
    assert [(c.name, c.data_type, c.nullable, c.position) for c in desc.columns] == [
        ("order_id", "bigint", False, 0),
        ("customer_id", "bigint", True, 1),
        ("order_date", "date", True, 2),
        ("status", "string", True, 3),
        ("total_amount", "decimal(12,2)", True, 4),
    ]
    assert desc.partition_columns == ["status"]
    pk = [c for c in desc.constraints if c.kind == "primary_key"]
    assert [c.columns for c in pk] == [["order_id"]]
    fk = [c for c in desc.constraints if c.kind == "foreign_key"]
    assert len(fk) == 1
    assert fk[0].columns == ["customer_id"]
    assert fk[0].ref_table is not None
    assert (fk[0].ref_table.schema_, fk[0].ref_table.name) == ("sales", "customers")
    assert fk[0].ref_columns == ["customer_id"]
    assert isinstance(desc.comment, UntrustedText)
    assert desc.comment.text == "Customer orders, one row per order"
    assert isinstance(desc.columns[0].comment, UntrustedText)
    assert desc.version.version == 1
    assert isinstance(desc.version.committed_at, datetime)
    assert desc.version.committed_at.tzinfo is not None
    assert desc.tags == ["Domain.Sales"]
    assert desc.owners == ["data-platform@example.com"]
    assert desc.storage_location is not None


async def test_fr_src_03_column_tags() -> None:
    desc = await DeltaStorageSource(_config()).describe_table(_ref("customers"))
    by_name = {c.name: c for c in desc.columns}
    assert by_name["email"].tags == ["PII.Email"]
    assert by_name["full_name"].tags == ["PII.Name"]
    assert by_name["country"].tags == []
    assert by_name["created_at"].data_type == "timestamp"


async def test_fr_src_04_get_table_version() -> None:
    version = await DeltaStorageSource(_config()).get_table_version(_ref("orders"))
    assert version.version == 1
    assert version.committed_at is not None


async def test_unknown_table_not_found() -> None:
    with pytest.raises(LakeloomError) as exc:
        await DeltaStorageSource(_config()).describe_table(_ref("nope"))
    assert exc.value.code == "not_found"


async def test_missing_uri_not_found(tmp_path: Path) -> None:
    source = DeltaStorageSource(_config(tmp_path, ("ghost",)))
    with pytest.raises(LakeloomError) as exc:
        await source.describe_table(_ref("ghost"))
    assert exc.value.code == "not_found"
    assert "LAKELOOM_CONFIG_FILE" in exc.value.hint


async def test_non_delta_dir_not_found(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "data.csv").write_text("a,b\n1,2\n")
    source = DeltaStorageSource(_config(tmp_path, ("plain",)))
    with pytest.raises(LakeloomError) as exc:
        await source.get_table_version(_ref("plain"))
    assert exc.value.code == "not_found"


async def test_profile_table_placeholder() -> None:
    from lakeloom.sources.base import ProfilePolicy

    policy = ProfilePolicy.from_settings(Settings())
    with pytest.raises(LakeloomError) as exc:
        await DeltaStorageSource(_config()).profile_table(_ref("orders"), policy)
    assert exc.value.code == "validation_error"


def test_missing_deltalake_extra_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] == "deltalake":
            raise ImportError("no deltalake")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(LakeloomError) as exc:
        DeltaStorageSource(_config())
    assert exc.value.code == "validation_error"
    assert "lakeloom[delta]" in exc.value.hint


def test_fr_src_06_registry_builds_delta_source() -> None:
    built = build_sources(SourcesConfig(sources=[_config()]), Settings())
    assert isinstance(built["fixture"], DeltaStorageSource)
