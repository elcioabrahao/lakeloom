"""Scan record repository (FR-SRC-10)."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.errors import LakeloomError
from lakeloom.sources.models import TableRef, TableVersion
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.scans import ScannedTable, ScanRecord, ScanRepository, new_scan_id


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield eng
    await eng.dispose()


def _record(source: str = "lake", created_at: datetime | None = None) -> ScanRecord:
    ref = TableRef(catalog=None, schema="sales", name="orders")
    return ScanRecord(
        id=new_scan_id(),
        source=source,
        created_at=created_at or datetime.now(UTC),
        principal_subject="agent-1",
        principal_type="agent",
        client="pytest",
        status="completed",
        tables=[
            ScannedTable(
                ref=ref,
                version=TableVersion(version=3, committed_at=datetime(2026, 1, 2, tzinfo=UTC)),
                om_fqn="svc.db.sales.orders",
                om_id="abc",
            )
        ],
    )


def test_scan_id_shape() -> None:
    a = new_scan_id()
    assert a.startswith("scan_") and len(a) == 31
    assert new_scan_id(now_ms=1) < new_scan_id(now_ms=2)


async def test_fr_src_10_scan_record_roundtrip(engine: AsyncEngine) -> None:
    repo = ScanRepository(engine)
    rec = _record()
    await repo.add(rec)
    got = await repo.get(rec.id)
    assert got == rec
    assert got.created_at.tzinfo is not None
    assert got.created_at.utcoffset() == timedelta(0)
    assert got.tables[0].version.committed_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert (await repo.latest_for_source("lake")) == rec
    assert (await repo.latest_for_source("other")) is None


async def test_list_paginates_newest_first(engine: AsyncEngine) -> None:
    repo = ScanRepository(engine)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    recs = []
    for i in range(5):
        rec = _record(created_at=base + timedelta(minutes=i)).model_copy(
            update={"id": new_scan_id(now_ms=int((base + timedelta(minutes=i)).timestamp() * 1000))}
        )
        recs.append(rec)
        await repo.add(rec)
    await repo.add(_record(source="other"))
    expected = [r.id for r in reversed(recs)]
    p1 = await repo.list("lake", None, 2)
    assert [r.id for r in p1.items] == expected[:2]
    assert p1.next_cursor == expected[1]
    p2 = await repo.list("lake", p1.next_cursor, 2)
    assert [r.id for r in p2.items] == expected[2:4]
    p3 = await repo.list("lake", p2.next_cursor, 2)
    assert [r.id for r in p3.items] == expected[4:]
    assert p3.next_cursor is None
    assert len((await repo.list(None, None, 50)).items) == 6
    assert (await repo.latest_for_source("lake")) == recs[-1]


async def test_get_missing_not_found(engine: AsyncEngine) -> None:
    with pytest.raises(LakeloomError) as exc:
        await ScanRepository(engine).get("scan_missing")
    assert exc.value.code == "not_found"
