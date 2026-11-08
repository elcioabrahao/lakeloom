# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingTypeStubs=false, reportAttributeAccessIssue=false
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.dataset as ds
import pytest
from deltalake import DeltaTable

from lakeloom.config import Settings
from lakeloom.sources.base import ProfilePolicy
from lakeloom.sources.config import DeltaTableEntry
from lakeloom.sources.delta_storage import DeltaStorageSource, _read_table
from lakeloom.sources.models import (
    ColumnInfo,
    ColumnProfile,
    TableDescription,
    TableProfile,
    TableRef,
    TableVersion,
)
from lakeloom.sources.profiling import profile_arrow_dataset

FIXTURE = Path(__file__).parents[2] / "fixtures" / "lake"


def _profile(table: str, **overrides: Any) -> TableProfile:
    policy = ProfilePolicy.from_settings(Settings()).model_copy(update=overrides)
    entry = DeltaTableEntry(uri=(FIXTURE / table).as_uri(), catalog="retail", schema="sales")
    ref = TableRef(catalog="retail", schema="sales", name=table)
    desc = _read_table(entry, ref, DeltaStorageSource.__name__)
    dataset = DeltaTable(entry.uri).to_pyarrow_dataset()
    return profile_arrow_dataset(dataset, desc, policy)


def _col(profile: TableProfile, name: str) -> ColumnProfile:
    return next(c for c in profile.columns if c.name == name)


def test_fr_src_08_orders_profile() -> None:
    p = _profile("orders")
    assert p.row_count == 1000
    assert _col(p, "customer_id").null_rate == 0.0
    total = _col(p, "total_amount")
    assert total.min is not None and total.max is not None
    assert float(total.min) <= float(total.max)
    assert _col(p, "status").distinct_count == 4
    assert _col(p, "order_date").min is not None


def test_fr_src_09_samples_off_by_default() -> None:
    status = _col(_profile("orders"), "status")
    assert status.top_k is None
    assert status.values_withheld == "samples_disabled"
    assert status.min is None and status.max is None


def test_fr_src_09_samples_on_top_k() -> None:
    status = _col(_profile("orders", samples=True), "status")
    assert status.top_k is not None
    assert len(status.top_k) == 4
    assert sum(c for _, c in status.top_k) == 1000
    assert status.top_k == sorted(status.top_k, key=lambda t: (-t[1], t[0]))
    assert status.values_withheld is None


def test_fr_src_09_sensitive_columns_withheld() -> None:
    p = _profile("customers", samples=True)
    for name in ("email", "full_name"):
        c = _col(p, name)
        assert c.top_k is None and c.min is None and c.max is None
        assert c.values_withheld == "sensitive"
    assert _col(p, "country").top_k


def test_fr_src_09_sensitive_withheld_even_when_samples_off() -> None:
    assert _col(_profile("customers"), "email").values_withheld == "sensitive"


def test_fr_src_09_values_truncated() -> None:
    p = _profile("orders", samples=True, value_max_length=3)
    top = _col(p, "status").top_k
    assert top
    assert all(len(v) <= 4 and (len(v) <= 3 or v.endswith("…")) for v, _ in top)
    assert any(v.endswith("…") for v, _ in top)


def test_high_cardinality_stops_tracking() -> None:
    p = _profile("orders", samples=True, low_cardinality=5)
    oid = _col(p, "order_id")
    assert oid.distinct_count is None and oid.top_k is None
    assert oid.min is not None


def _desc(columns: list[ColumnInfo]) -> TableDescription:
    return TableDescription(
        ref=TableRef(schema="s", name="t"), columns=columns, version=TableVersion()
    )


def test_empty_table_null_rate_none() -> None:
    table = pa.table({"a": pa.array([], type=pa.int64())})
    desc = _desc([ColumnInfo(name="a", data_type="bigint", nullable=True, position=0)])
    p = profile_arrow_dataset(ds.dataset(table), desc, ProfilePolicy.from_settings(Settings()))
    assert p.row_count == 0
    assert p.columns[0].null_rate is None


def test_sensitive_numeric_minmax_withheld_and_nulls_excluded() -> None:
    table = pa.table({"yob": [1980, 1990, None], "s": ["a", None, "a"]})
    desc = _desc(
        [
            ColumnInfo(name="yob", data_type="bigint", nullable=True, tags=["PII.Age"], position=0),
            ColumnInfo(name="s", data_type="string", nullable=True, position=1),
        ]
    )
    policy = ProfilePolicy.from_settings(Settings()).model_copy(update={"samples": True})
    p = profile_arrow_dataset(ds.dataset(table), desc, policy)
    yob, s = p.columns
    assert yob.min is None and yob.max is None and yob.top_k is None
    assert yob.null_rate == pytest.approx(1 / 3)
    assert s.top_k == [("a", 2)]
    assert s.distinct_count == 1


def test_profile_streams_batches() -> None:
    n = 300_000
    table = pa.table({"id": pa.array(range(n)), "g": pa.array([f"g{i % 7}" for i in range(n)])})
    dataset = ds.dataset(table)
    desc = _desc(
        [
            ColumnInfo(name="id", data_type="bigint", nullable=False, position=0),
            ColumnInfo(name="g", data_type="string", nullable=True, position=1),
        ]
    )
    calls: list[int] = []

    class Spy:
        schema = dataset.schema

        def to_batches(self, **kwargs: Any) -> Any:
            calls.append(kwargs["batch_size"])
            return dataset.to_batches(**kwargs)

        def to_table(self, **kwargs: Any) -> Any:
            raise AssertionError("to_table must not be used")

    policy = ProfilePolicy.from_settings(Settings()).model_copy(update={"samples": True})
    p = profile_arrow_dataset(Spy(), desc, policy, batch_size=1000)  # type: ignore[arg-type]
    assert p.row_count == n
    assert calls == [1000]
    assert _col(p, "g").distinct_count == 7


@pytest.mark.parametrize("tag", ["pii.email", "Pii.Email", " PII.Email ", "PII.EMAIL"])
def test_fr_src_09_sensitive_tag_match_is_case_and_space_insensitive(tag: str) -> None:
    table = pa.table({"e": ["a@x.com", "b@x.com"]})
    desc = _desc([ColumnInfo(name="e", data_type="string", nullable=True, tags=[tag], position=0)])
    policy = ProfilePolicy.from_settings(Settings()).model_copy(update={"samples": True})
    c = profile_arrow_dataset(ds.dataset(table), desc, policy).columns[0]
    assert c.values_withheld == "sensitive"
    assert c.top_k is None and c.min is None and c.max is None
