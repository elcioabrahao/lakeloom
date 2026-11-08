"""Register the fixture lake's tables in Unity Catalog OSS (FR-SRC-02). Idempotent.

Usage::

    uv run python scripts/seed_uc_fixture.py --uc-url http://localhost:8081 \\
        [--lake tests/fixtures/lake] [--storage-root file:///lake]

Creates catalog ``retail``, schema ``sales`` and four EXTERNAL DELTA tables whose
``storage_location`` is ``<storage-root>/<table>``. Columns, comments, partitioning and the
``lakeloom.*`` conventions are read from the Delta tables with the package's own connector,
so UC and the lake cannot drift. UC OSS v0.7.0 has no tag or constraint API, so tags,
owners and keys travel as table ``properties`` (ADR 0008): ``lakeloom.tags``,
``lakeloom.owners``, ``lakeloom.primaryKey``, ``lakeloom.foreignKey.<column>`` and
``lakeloom.columnTags.<column>``. Re-runs delete and recreate each table; the script waits
for UC to answer (up to ``--wait`` seconds) so compose can start it right away.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from lakeloom.sources.config import DeltaSourceConfig, DeltaTableEntry
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.sources.models import ColumnInfo, TableDescription, TableRef

CATALOG = "retail"
SCHEMA = "sales"
TABLES = ("customers", "products", "orders", "invoices")
API = "/api/2.1/unity-catalog"

# Spark type text -> (UC ColumnTypeName, Delta JSON type)
_TYPES = {
    "bigint": ("LONG", "long"),
    "int": ("INT", "integer"),
    "smallint": ("SHORT", "short"),
    "tinyint": ("BYTE", "byte"),
    "string": ("STRING", "string"),
    "boolean": ("BOOLEAN", "boolean"),
    "double": ("DOUBLE", "double"),
    "float": ("FLOAT", "float"),
    "date": ("DATE", "date"),
    "timestamp": ("TIMESTAMP", "timestamp"),
    "timestamp_ntz": ("TIMESTAMP_NTZ", "timestamp_ntz"),
    "binary": ("BINARY", "binary"),
}


def uc_column(column: ColumnInfo, partition_columns: list[str]) -> dict[str, Any]:
    """Map a connector column onto a UC ``ColumnInfo``."""
    spark = column.data_type.lower()
    out: dict[str, Any] = {
        "name": column.name,
        "type_text": column.data_type,
        "position": column.position,
        "nullable": column.nullable,
        "partition_index": (
            partition_columns.index(column.name) if column.name in partition_columns else None
        ),
    }
    decimal = re.fullmatch(r"decimal\((\d+),\s*(\d+)\)", spark)
    if decimal:
        type_name, json_type = "DECIMAL", spark.replace(" ", "")
        out.update(type_precision=int(decimal[1]), type_scale=int(decimal[2]))
    else:
        type_name, json_type = _TYPES.get(spark, ("STRING", "string"))
    out["type_name"] = type_name
    out["type_json"] = json.dumps(
        {"name": column.name, "type": json_type, "nullable": column.nullable, "metadata": {}}
    )
    if column.comment is not None:
        out["comment"] = column.comment.text
    return out


def table_payload(desc: TableDescription, storage_root: str) -> dict[str, Any]:
    properties: dict[str, str] = dict(desc.properties)
    if desc.tags:
        properties["lakeloom.tags"] = ",".join(desc.tags)
    if desc.owners:
        properties["lakeloom.owners"] = ",".join(desc.owners)
    for constraint in desc.constraints:
        if constraint.kind == "primary_key":
            properties["lakeloom.primaryKey"] = ",".join(constraint.columns)
        elif constraint.ref_table is not None:
            target = f"{constraint.ref_table.schema_}.{constraint.ref_table.name}"
            properties[f"lakeloom.foreignKey.{constraint.columns[0]}"] = (
                f"{target}.{constraint.ref_columns[0]}"
            )
    for column in desc.columns:
        if column.tags:
            properties[f"lakeloom.columnTags.{column.name}"] = ",".join(column.tags)
    payload: dict[str, Any] = {
        "name": desc.ref.name,
        "catalog_name": CATALOG,
        "schema_name": SCHEMA,
        "table_type": "EXTERNAL",
        "data_source_format": "DELTA",
        "storage_location": f"{storage_root.rstrip('/')}/{desc.ref.name}",
        "columns": [uc_column(c, desc.partition_columns) for c in desc.columns],
        "properties": properties,
    }
    if desc.comment is not None:
        payload["comment"] = desc.comment.text
    return payload


async def read_tables(lake: Path) -> list[TableDescription]:
    entries = [
        DeltaTableEntry(uri=str(lake / t), catalog=CATALOG, schema=SCHEMA, name=t) for t in TABLES
    ]
    source = DeltaStorageSource(DeltaSourceConfig(type="delta", name="fixture", tables=entries))
    refs = [TableRef(catalog=CATALOG, schema=SCHEMA, name=t) for t in TABLES]
    return [await source.describe_table(r) for r in refs]


def wait_for_uc(http: httpx.Client, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if http.get(f"{API}/catalogs").status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"Unity Catalog did not answer within {seconds:.0f}s")
        time.sleep(2)


def _create(http: httpx.Client, path: str, body: dict[str, Any]) -> None:
    response = http.post(f"{API}{path}", json=body)
    if response.status_code == 409 or "ALREADY_EXISTS" in response.text:
        return
    if response.status_code >= 400:
        raise SystemExit(f"POST {path} failed: HTTP {response.status_code} {response.text[:500]}")


def seed(uc_url: str, tables: list[TableDescription], storage_root: str, wait: float) -> None:
    with httpx.Client(base_url=uc_url.rstrip("/"), timeout=30) as http:
        wait_for_uc(http, wait)
        _create(http, "/catalogs", {"name": CATALOG, "comment": "Lakeloom fixture lake"})
        _create(http, "/schemas", {"name": SCHEMA, "catalog_name": CATALOG})
        for desc in tables:
            full = f"{CATALOG}.{SCHEMA}.{desc.ref.name}"
            deleted = http.delete(f"{API}/tables/{full}")
            if deleted.status_code not in (200, 404):
                raise SystemExit(f"DELETE {full} failed: HTTP {deleted.status_code}")
            _create(http, "/tables", table_payload(desc, storage_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--uc-url", required=True)
    parser.add_argument("--lake", type=Path, default=Path("tests/fixtures/lake"))
    parser.add_argument("--storage-root", default="file:///lake")
    parser.add_argument("--wait", type=float, default=120)
    args = parser.parse_args(argv)
    seed(args.uc_url, asyncio.run(read_tables(args.lake)), args.storage_root, args.wait)
    for name in TABLES:
        print(f"{CATALOG}.{SCHEMA}.{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
