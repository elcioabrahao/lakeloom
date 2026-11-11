"""Alembic migrations on the state store (TC-9)."""

import asyncio
import os
from importlib.resources import as_file, files
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from lakeloom.state.db import create_engine, upgrade


async def _tables(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: inspect(c).get_table_names())


async def test_tc_9_upgrade_creates_scan_records(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    try:
        await upgrade(engine)
        assert "scan_records" in await _tables(engine)
        await upgrade(engine)  # second call is a no-op
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT count(*) FROM alembic_version"))).scalar_one()
        assert rows == 1
    finally:
        await engine.dispose()


async def test_0002_creates_proposals_and_audit_log(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    try:
        await upgrade(engine)
        tables = await _tables(engine)
        assert {"proposals", "audit_log"} <= set(tables)
        async with engine.connect() as conn:
            idx = await conn.run_sync(
                lambda c: {
                    t: {i["name"] for i in inspect(c).get_indexes(t)}
                    for t in ("proposals", "audit_log")
                }
            )
        assert "ix_proposals_model_status" in idx["proposals"]
        assert "ix_audit_log_at" in idx["audit_log"]
    finally:
        await engine.dispose()


async def test_0002_downgrade_drops_tables_keeps_0001(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    try:
        await upgrade(engine)

        def down(conn: Connection) -> None:
            with as_file(files("lakeloom.state") / "migrations") as script_dir:
                cfg = Config()
                cfg.set_main_option("script_location", str(script_dir))
                cfg.attributes["connection"] = conn
                command.downgrade(cfg, "0001")

        async with engine.begin() as conn:
            await conn.run_sync(down)
        tables = set(await _tables(engine))
        assert "scan_records" in tables
        assert not {"proposals", "audit_log"} & tables
    finally:
        await engine.dispose()


@pytest.mark.contract
async def test_upgrade_postgres() -> None:
    url = os.environ.get("LAKELOOM_TEST_DB_URL")
    if not url:
        pytest.skip("LAKELOOM_TEST_DB_URL not set")
    engine = create_engine(url)
    try:
        await upgrade(engine)
        await upgrade(engine)
        assert {"scan_records", "proposals", "audit_log"} <= set(await _tables(engine))
    finally:
        await engine.dispose()


@pytest.mark.contract
async def test_concurrent_upgrades_postgres() -> None:
    url = os.environ.get("LAKELOOM_TEST_DB_URL")
    if not url:
        pytest.skip("LAKELOOM_TEST_DB_URL not set")
    engines = [create_engine(url) for _ in range(4)]
    try:
        async with engines[0].begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS scan_records"))
            await conn.execute(text("DROP TABLE IF EXISTS proposals"))
            await conn.execute(text("DROP TABLE IF EXISTS audit_log"))
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await asyncio.gather(*(upgrade(e) for e in engines))
        assert "scan_records" in await _tables(engines[0])
    finally:
        for e in engines:
            await e.dispose()


@pytest.mark.contract
async def test_postgres_audit_oversized_client_is_stored() -> None:
    import uuid
    from datetime import UTC, datetime

    from lakeloom.core.audit import AuditRecord
    from lakeloom.state.audit import AuditRepository

    url = os.environ.get("LAKELOOM_TEST_DB_URL")
    if not url:
        pytest.skip("LAKELOOM_TEST_DB_URL not set")
    engine = create_engine(url)
    try:
        await upgrade(engine)
        marker = f"pg-{uuid.uuid4().hex}"
        rec = AuditRecord(
            at=datetime.now(UTC),
            principal_subject=marker + "s" * 400,
            principal_type="agent",
            client="c\x00" + "c" * 300,  # NUL is stripped (PostgreSQL text rejects it)
            tool="t" * 200,
            model="m" * 300,
            proposal_id="p" * 100,
            change_set_id="x" * 300,
            result="ok",
            error_code="e" * 100,
        )
        await AuditRepository(engine).add(rec)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT client, tool, principal_subject FROM audit_log "
                        "WHERE principal_subject LIKE :m"
                    ),
                    {"m": marker + "%"},
                )
            ).one()
        assert row.client == "c" * 255  # the inserted record itself, NUL gone, cut to width
        assert len(row.tool) == 128 and len(row.principal_subject) == 255
    finally:
        await engine.dispose()
