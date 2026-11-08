"""Scan service (FR-SRC-07, FR-SRC-10, NFR-PERF-02)."""

import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.config import Settings
from lakeloom.core.scans import ScanService, summarize
from lakeloom.om.client import OMUnavailable
from lakeloom.om.models import OMTable
from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.sources.config import DeltaSourceConfig, DeltaTableEntry
from lakeloom.sources.delta_storage import DeltaStorageSource
from lakeloom.state.db import create_engine, upgrade
from lakeloom.state.scans import ScanRepository

FIXTURE = Path(__file__).parents[2] / "fixtures" / "lake"
NAMES = ("customers", "products", "orders", "invoices")


class FakeOM:
    def __init__(self, known: set[str] | None = None, down: bool = False) -> None:
        self.known = known or set()
        self.down = down
        self.calls: list[str] = []

    async def get_table_by_fqn(self, fqn: str) -> OMTable | None:
        self.calls.append(fqn)
        if self.down:
            raise OMUnavailable("secret-url down")
        if fqn in self.known:
            return OMTable.model_validate({"id": "om-1", "fullyQualifiedName": fqn})
        return None


def _principal() -> Principal:
    return Principal(
        subject="agent-7",
        type=PrincipalType.AGENT,
        client_id="c",
        username=None,
        scopes=frozenset(),
        groups=frozenset(),
        token=SecretStr("t"),
    )


def _config(extra: list[str] | None = None, om_service: str | None = None) -> DeltaSourceConfig:
    uris = [(FIXTURE / n).as_uri() for n in NAMES] + (extra or [])
    return DeltaSourceConfig(
        type="delta",
        name="fixture",
        om_service=om_service,
        tables=[DeltaTableEntry(uri=u, catalog="retail", schema="sales") for u in uris],
    )


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncGenerator[ScanRepository]:
    eng: AsyncEngine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    await upgrade(eng)
    yield ScanRepository(eng)
    await eng.dispose()


def _service(
    repo: ScanRepository, config: DeltaSourceConfig, om: FakeOM | None = None, **settings: object
) -> ScanService:
    return ScanService(
        {config.name: DeltaStorageSource(config)},
        repo,
        om,  # pyright: ignore[reportArgumentType]
        Settings(**settings),  # pyright: ignore[reportArgumentType]
    )


async def test_fr_src_10_scan_fixture_lake_records_versions(repo: ScanRepository) -> None:
    svc = _service(repo, _config())
    rec = await svc.scan("fixture", principal=_principal(), client="pytest")
    assert rec.status == "completed"
    assert len(rec.tables) == 4
    assert all(t.version.version is not None for t in rec.tables)
    orders = next(t for t in rec.tables if t.ref.name == "orders")
    assert orders.version.version == 1
    got = await repo.get(rec.id)
    assert got.principal_subject == "agent-7" and got.principal_type == "agent"
    assert got.client == "pytest"
    assert len(got.tables) == 4


async def test_scan_with_profile_respects_policy(repo: ScanRepository) -> None:
    svc = _service(repo, _config())
    rec = await svc.scan("fixture", profile=True, principal=_principal(), client=None)
    orders = next(t for t in rec.tables if t.ref.name == "orders")
    assert orders.profile is not None
    assert all(c.top_k is None for c in orders.profile.columns)


async def test_fr_src_07_om_lookup_sets_fqn(repo: ScanRepository) -> None:
    om = FakeOM({"lakeloom_fixture.retail.sales.orders"})
    svc = _service(repo, _config(om_service="lakeloom_fixture"), om)
    rec = await svc.scan("fixture", principal=_principal(), client=None)
    by = {t.ref.name: t for t in rec.tables}
    assert by["orders"].om_fqn == "lakeloom_fixture.retail.sales.orders"
    assert by["orders"].om_id == "om-1"
    assert by["customers"].om_fqn is None
    assert rec.warnings == []


async def test_scan_partial_on_missing_table(repo: ScanRepository, tmp_path: Path) -> None:
    bogus = (tmp_path / "bogus").as_uri()
    svc = _service(repo, _config([bogus]))
    rec = await svc.scan("fixture", principal=_principal(), client=None)
    assert rec.status == "partial"
    failed = [t for t in rec.tables if t.error]
    assert len(failed) == 1 and failed[0].ref.name == "bogus"
    assert failed[0].error is not None and failed[0].error["code"] == "not_found"
    assert sum(t.error is None for t in rec.tables) == 4
    text = summarize(rec)
    assert "bogus" in text and "not_found" in text


async def test_all_tables_failed_is_failed(repo: ScanRepository, tmp_path: Path) -> None:
    cfg = DeltaSourceConfig(
        type="delta",
        name="fixture",
        tables=[DeltaTableEntry(uri=(tmp_path / "x").as_uri(), schema="s")],
    )
    rec = await _service(repo, cfg).scan("fixture", principal=_principal(), client=None)
    assert rec.status == "failed"


async def test_om_down_does_not_fail_scan(repo: ScanRepository) -> None:
    om = FakeOM(down=True)
    svc = _service(repo, _config(om_service="svc"), om)
    rec = await svc.scan("fixture", principal=_principal(), client=None)
    assert rec.status == "completed"
    assert all(t.om_fqn is None for t in rec.tables)
    assert len(rec.warnings) == 4
    assert "OpenMetadata lookup failed for retail.sales.orders: upstream_unavailable" in (
        rec.warnings
    )
    assert not any("secret-url" in w for w in rec.warnings)


async def test_unknown_source_is_not_found(repo: ScanRepository) -> None:
    from lakeloom.errors import LakeloomError

    svc = _service(repo, _config())
    with pytest.raises(LakeloomError) as info:
        await svc.scan("nope", principal=_principal(), client=None)
    assert info.value.code == "not_found"


async def test_filters(repo: ScanRepository) -> None:
    svc = _service(repo, _config())
    p = _principal()
    rec = await svc.scan(
        "fixture", tables=["orders", "retail.sales.customers"], principal=p, client=None
    )
    assert sorted(t.ref.name for t in rec.tables) == ["customers", "orders"]
    rec = await svc.scan(
        "fixture", schemas=["sales"], catalogs=["retail"], principal=p, client=None
    )
    assert len(rec.tables) == 4


async def test_filter_matching_nothing_fails(repo: ScanRepository) -> None:
    svc = _service(repo, _config())
    rec = await svc.scan("fixture", tables=["ghost"], principal=_principal(), client=None)
    assert rec.status == "failed"
    assert rec.tables == []
    assert "ghost" in summarize(rec)
    assert (await repo.get(rec.id)).status == "failed"


async def test_summarize_omits_comments(repo: ScanRepository) -> None:
    rec = await _service(repo, _config()).scan("fixture", principal=_principal(), client=None)
    text = summarize(rec)
    assert "orders" in text and "version 1" in text
    for t in rec.tables:
        assert t.description is not None
        for c in t.description.columns:
            if c.comment:
                assert c.comment.text not in text
        if t.description.comment:
            assert t.description.comment.text not in text


async def test_nfr_perf_02_100_tables_under_60s(repo: ScanRepository, tmp_path: Path) -> None:
    import pyarrow as pa
    from deltalake import write_deltalake

    entries: list[DeltaTableEntry] = []
    for i in range(100):
        path = tmp_path / "lake" / f"t{i:03d}"
        write_deltalake(str(path), pa.table({"id": [i]}))
        entries.append(DeltaTableEntry(uri=path.as_uri(), catalog="c", schema="s"))
    cfg = DeltaSourceConfig(type="delta", name="big", tables=entries)
    svc = _service(repo, cfg)
    start = time.monotonic()
    rec = await svc.scan("big", principal=_principal(), client=None)
    elapsed = time.monotonic() - start
    print(f"perf: 100 tables scanned in {elapsed:.2f}s")
    assert rec.status == "completed" and len(rec.tables) == 100
    assert elapsed < 60
