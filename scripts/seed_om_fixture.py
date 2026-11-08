"""Seed the fixture lake's tables into OpenMetadata (FR-SRC-07, FR-OM-01). Idempotent.

Usage::

    uv run python scripts/seed_om_fixture.py --om-url http://localhost:8585 \\
        --token-file /secrets/om_bot_token [--lake tests/fixtures/lake]

Creates database service ``lakeloom_fixture`` (type ``UnityCatalog``), database ``retail``,
schema ``sales`` and one table per fixture Delta table, using PUT create-or-update so a
second run updates in place and keeps the same ids. Table metadata is read with the
package's own Delta connector, so OM and the lake cannot drift. The bot token is read from
the token file and only ever sent in the Authorization header; it is never printed.

Tag mapping: OM's built-in ``PII`` classification has only ``Sensitive`` and
``NonSensitive``, so fixture tags ``PII.*`` map to ``PII.Sensitive``. Other fixture tags
(``Domain.*``) are not OM classifications and are not seeded.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from lakeloom.sources.config import DeltaSourceConfig, DeltaTableEntry
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.sources.models import ColumnInfo, TableDescription, TableRef

SERVICE = "lakeloom_fixture"
SERVICE_TYPE = "UnityCatalog"  # present in the 2.1.0-rc1 serviceType enum
DATABASE = "retail"
SCHEMA = "sales"
TABLES = ("customers", "products", "orders", "invoices")
PII_TAG = "PII.Sensitive"

_SIMPLE = {
    "bigint": "BIGINT",
    "int": "INT",
    "smallint": "SMALLINT",
    "tinyint": "TINYINT",
    "string": "STRING",
    "boolean": "BOOLEAN",
    "double": "DOUBLE",
    "float": "FLOAT",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "timestamp_ntz": "TIMESTAMP",
    "binary": "BINARY",
}


def om_column(column: ColumnInfo) -> dict[str, Any]:
    """Map a connector column onto an OM ``Column`` (dataType enum from the spec)."""
    spark = column.data_type.lower()
    out: dict[str, Any] = {
        "name": column.name,
        "dataTypeDisplay": column.data_type,
        "ordinalPosition": column.position + 1,
    }
    decimal = re.fullmatch(r"decimal\((\d+),\s*(\d+)\)", spark)
    if decimal:
        out.update(dataType="DECIMAL", precision=int(decimal[1]), scale=int(decimal[2]))
    elif spark in _SIMPLE:
        out["dataType"] = _SIMPLE[spark]
    else:
        out["dataType"] = "UNKNOWN"
    if column.comment is not None:
        out["description"] = column.comment.text
    if any(tag.upper().startswith("PII.") for tag in column.tags):
        out["tags"] = [
            {
                "tagFQN": PII_TAG,
                "source": "Classification",
                "labelType": "Manual",
                "state": "Confirmed",
            }
        ]
    return out


def _fqn(*parts: str) -> str:
    return ".".join(parts)


def table_payload(desc: TableDescription) -> dict[str, Any]:
    name = desc.ref.name
    constraints: list[dict[str, Any]] = []
    for c in desc.constraints:
        if c.kind == "primary_key":
            constraints.append({"constraintType": "PRIMARY_KEY", "columns": c.columns})
        elif c.ref_table is not None:
            target = _fqn(SERVICE, DATABASE, c.ref_table.schema_, c.ref_table.name)
            constraints.append(
                {
                    "constraintType": "FOREIGN_KEY",
                    "columns": c.columns,
                    "referredColumns": [_fqn(target, col) for col in c.ref_columns],
                    "relationshipType": "MANY_TO_ONE",
                }
            )
    payload: dict[str, Any] = {
        "name": name,
        "databaseSchema": _fqn(SERVICE, DATABASE, SCHEMA),
        "tableType": "Regular",
        "columns": [om_column(c) for c in desc.columns],
        "tableConstraints": constraints,
    }
    if desc.comment is not None:
        payload["description"] = desc.comment.text
    return payload


async def read_tables(lake: Path) -> list[TableDescription]:
    entries = [
        DeltaTableEntry(uri=str(lake / t), catalog=DATABASE, schema=SCHEMA, name=t) for t in TABLES
    ]
    source = DeltaStorageSource(DeltaSourceConfig(type="delta", name="fixture", tables=entries))
    refs = [TableRef(catalog=DATABASE, schema=SCHEMA, name=t) for t in TABLES]
    return [await source.describe_table(r) for r in refs]


def _put(http: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = http.put(path, json=body)
    if response.status_code >= 400:
        raise SystemExit(f"PUT {path} failed: HTTP {response.status_code} {response.text[:500]}")
    return response.json()


def seed(om_url: str, token: str, tables: list[TableDescription]) -> dict[str, str]:
    """Create-or-update everything; return ``{table name: id}``."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(base_url=f"{om_url.rstrip('/')}/api", headers=headers, timeout=30) as http:
        _put(
            http,
            "/v1/services/databaseServices",
            {
                "name": SERVICE,
                "serviceType": SERVICE_TYPE,
                "description": "Lakeloom fixture lake (tests/fixtures/lake)",
            },
        )
        _put(http, "/v1/databases", {"name": DATABASE, "service": SERVICE})
        _put(
            http,
            "/v1/databaseSchemas",
            {"name": SCHEMA, "database": _fqn(SERVICE, DATABASE)},
        )
        ids: dict[str, str] = {}
        # Dependency order: a foreign key target must exist before the referring table.
        for desc in sorted(tables, key=lambda d: (len(d.constraints), d.ref.name)):
            if any(c.kind == "foreign_key" for c in desc.constraints):
                continue
            ids[desc.ref.name] = str(_put(http, "/v1/tables", table_payload(desc))["id"])
        pending = [d for d in tables if d.ref.name not in ids]
        for _ in range(len(pending)):
            for desc in list(pending):
                targets = {c.ref_table.name for c in desc.constraints if c.ref_table}
                if targets <= ids.keys():
                    ids[desc.ref.name] = str(_put(http, "/v1/tables", table_payload(desc))["id"])
                    pending.remove(desc)
        if pending:
            raise SystemExit("unresolved foreign-key order among fixture tables")
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--om-url", required=True)
    parser.add_argument("--token-file", type=Path, default=Path("/secrets/om_bot_token"))
    parser.add_argument("--lake", type=Path, default=Path("tests/fixtures/lake"))
    args = parser.parse_args(argv)
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"token file {args.token_file} is empty")
    ids = seed(args.om_url, token, asyncio.run(read_tables(args.lake)))
    for name in TABLES:
        print(f"{SERVICE}.{DATABASE}.{SCHEMA}.{name} {ids[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
