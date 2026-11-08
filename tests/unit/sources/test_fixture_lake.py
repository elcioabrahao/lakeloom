import json
import runpy
from pathlib import Path
from typing import Any

from deltalake import DeltaTable

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "lake"
TABLES = ("customers", "products", "orders", "invoices")


def _summary(path: Path) -> dict[str, Any]:
    table = DeltaTable(str(path))
    meta = table.metadata()
    return {
        "schema": json.loads(table.schema().to_json()),
        "rows": table.to_pyarrow_table().num_rows,
        "version": table.version(),
        "description": meta.description,
        "configuration": meta.configuration,
        "partition_columns": meta.partition_columns,
    }


def test_nfr_tst_03_fixture_lake_regenerates_same_schema(tmp_path: Path) -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "make_fixture_lake.py"))
    module["generate"](tmp_path / "lake")
    for name in TABLES:
        assert _summary(tmp_path / "lake" / name) == _summary(FIXTURE / name), name


def test_nfr_tst_03_fixture_lake_shape() -> None:
    rows = {name: _summary(FIXTURE / name)["rows"] for name in TABLES}
    assert rows == {"customers": 200, "products": 50, "orders": 1000, "invoices": 1000}
    assert _summary(FIXTURE / "orders")["version"] == 1
