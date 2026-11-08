"""Generate the committed fixture lake (NFR-TST-03): four small Delta tables.

Usage: ``uv run python scripts/make_fixture_lake.py --out tests/fixtures/lake``

Data is deterministic (fixed seed). Metadata follows the conventions in ADR 0008.
"""

import argparse
import json
import random
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
from deltalake import write_deltalake

SEED = 20261108
COUNTRIES = ["BR", "US", "DE", "FR", "JP"]
CATEGORIES = ["Books", "Toys", "Garden", "Kitchen", "Sports", "Office"]
STATUSES = ["pending", "paid", "shipped", "cancelled"]
EPOCH = datetime(2025, 1, 1, tzinfo=UTC)
TS = pa.timestamp("us", tz="UTC")


def _field(
    name: str, typ: pa.DataType, comment: str, *, nullable: bool = True, tags: str | None = None
) -> pa.Field:
    meta = {"comment": comment}
    if tags:
        meta["lakeloom.tags"] = tags
    return pa.field(name, typ, nullable=nullable, metadata=meta)


def _write(
    out: Path,
    name: str,
    schema: pa.Schema,
    rows: dict[str, list[Any]],
    description: str,
    configuration: dict[str, str],
    *,
    partition_by: list[str] | None = None,
    split: int | None = None,
) -> None:
    uri = str(out / name)
    config = {"lakeloom.owners": "data-platform@example.com", **configuration}
    table = pa.table(rows, schema=schema)
    first = table if split is None else table.slice(0, split)
    write_deltalake(
        uri,
        first,
        mode="overwrite",
        partition_by=partition_by,
        name=name,
        description=description,
    )
    if split is not None:
        write_deltalake(uri, table.slice(split), mode="append")
    _set_configuration(Path(uri), config)


def _set_configuration(table_dir: Path, configuration: dict[str, str]) -> None:
    """Add ``lakeloom.*`` keys to the metaData action of commit 0.

    delta-rs 1.6 (delta-kernel) rejects unknown table properties on write, but readers
    accept them (Spark writes them freely), so the keys are patched into the log. The
    log is the source of truth, exactly as for a table written by another engine.
    """
    commit = table_dir / "_delta_log" / f"{0:020d}.json"
    lines = commit.read_text(encoding="utf-8").splitlines()
    patched: list[str] = []
    for line in lines:
        action = json.loads(line)
        if "metaData" in action:
            action["metaData"]["configuration"].update(configuration)
        patched.append(json.dumps(action, separators=(",", ":")))
    commit.write_text("\n".join(patched) + "\n", encoding="utf-8")


def generate(out: Path) -> None:
    rng = random.Random(SEED)  # noqa: S311 - deterministic fixture data, not security
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    n_cust, n_prod, n_ord = 200, 50, 1000
    customers = {
        "customer_id": list(range(1, n_cust + 1)),
        "email": [f"customer{i}@example.com" for i in range(1, n_cust + 1)],
        "full_name": [f"Customer {i:03d}" for i in range(1, n_cust + 1)],
        "country": [rng.choice(COUNTRIES) for _ in range(n_cust)],
        "created_at": [EPOCH + timedelta(hours=rng.randrange(0, 8000)) for _ in range(n_cust)],
    }
    _write(
        out,
        "customers",
        pa.schema(
            [
                _field("customer_id", pa.int64(), "Unique customer identifier", nullable=False),
                _field("email", pa.string(), "Customer contact email address", tags="PII.Email"),
                _field("full_name", pa.string(), "Customer full legal name", tags="PII.Name"),
                _field("country", pa.string(), "ISO country code of the customer"),
                _field("created_at", TS, "When the customer account was created"),
            ]
        ),
        customers,
        "Registered customers of the retail business",
        {"lakeloom.primaryKey": "customer_id", "lakeloom.tags": "Domain.Sales"},
    )

    products = {
        "product_id": list(range(1, n_prod + 1)),
        "sku": [f"SKU-{i:05d}" for i in range(1, n_prod + 1)],
        "name": [f"Product {i:02d}" for i in range(1, n_prod + 1)],
        "category": [rng.choice(CATEGORIES) for _ in range(n_prod)],
        "unit_price": [Decimal(rng.randrange(100, 50000)) / 100 for _ in range(n_prod)],
    }
    _write(
        out,
        "products",
        pa.schema(
            [
                _field("product_id", pa.int64(), "Unique product identifier", nullable=False),
                _field("sku", pa.string(), "Stock keeping unit code"),
                _field("name", pa.string(), "Product display name"),
                _field("category", pa.string(), "Merchandising category of the product"),
                _field("unit_price", pa.decimal128(10, 2), "List price per unit"),
            ]
        ),
        products,
        "Catalogue of products sold",
        {"lakeloom.primaryKey": "product_id", "lakeloom.tags": "Domain.Sales"},
    )

    orders = {
        "order_id": list(range(1, n_ord + 1)),
        "customer_id": [rng.randrange(1, n_cust + 1) for _ in range(n_ord)],
        "order_date": [
            date(2025, 1, 1) + timedelta(days=rng.randrange(0, 365)) for _ in range(n_ord)
        ],
        "status": [rng.choice(STATUSES) for _ in range(n_ord)],
        "total_amount": [Decimal(rng.randrange(500, 500000)) / 100 for _ in range(n_ord)],
    }
    _write(
        out,
        "orders",
        pa.schema(
            [
                _field("order_id", pa.int64(), "Unique order identifier", nullable=False),
                _field("customer_id", pa.int64(), "Customer who placed the order"),
                _field("order_date", pa.date32(), "Calendar date the order was placed"),
                _field("status", pa.string(), "Fulfilment status of the order"),
                _field("total_amount", pa.decimal128(12, 2), "Order total including tax"),
            ]
        ),
        orders,
        "Customer orders, one row per order",
        {
            "lakeloom.primaryKey": "order_id",
            "lakeloom.foreignKey.customer_id": "sales.customers.customer_id",
            "lakeloom.tags": "Domain.Sales",
        },
        partition_by=["status"],
        split=n_ord // 2,  # two commits: initial write + append -> Delta version 1
    )

    invoices = {
        "invoice_id": list(range(1, n_ord + 1)),
        "order_id": list(range(1, n_ord + 1)),
        "issued_at": [EPOCH + timedelta(minutes=rng.randrange(0, 500000)) for _ in range(n_ord)],
        "amount": [Decimal(rng.randrange(500, 500000)) / 100 for _ in range(n_ord)],
        "currency": ["USD"] * n_ord,
    }
    _write(
        out,
        "invoices",
        pa.schema(
            [
                _field("invoice_id", pa.int64(), "Unique invoice identifier", nullable=False),
                _field("order_id", pa.int64(), "Order this invoice bills"),
                _field("issued_at", TS, "When the invoice was issued"),
                _field("amount", pa.decimal128(12, 2), "Invoiced amount"),
                _field("currency", pa.string(), "ISO currency code of the invoice"),
            ]
        ),
        invoices,
        "Invoices issued for orders",
        {
            "lakeloom.primaryKey": "invoice_id",
            "lakeloom.foreignKey.order_id": "sales.orders.order_id",
            "lakeloom.tags": "Domain.Finance",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="output directory (recreated)")
    generate(parser.parse_args().out)


if __name__ == "__main__":
    main()
