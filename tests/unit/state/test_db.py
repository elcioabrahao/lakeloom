"""State store engine helpers (NFR-OPS-01)."""

import os
from pathlib import Path

import pytest

from lakeloom.state.db import create_engine, ping, to_async_url


def test_to_async_url() -> None:
    assert to_async_url("sqlite:////data/x.db") == "sqlite+aiosqlite:////data/x.db"
    assert to_async_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert to_async_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert to_async_url("sqlite+aiosqlite:///x.db") == "sqlite+aiosqlite:///x.db"


async def test_ping_sqlite_in_tmp_path(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "lakeloom.db"
    engine = create_engine(f"sqlite:///{db}")
    try:
        await ping(engine)
        assert db.parent.is_dir()
    finally:
        await engine.dispose()


@pytest.mark.contract
async def test_ping_postgres() -> None:
    url = os.environ.get("LAKELOOM_TEST_DB_URL")
    if not url:
        pytest.skip("LAKELOOM_TEST_DB_URL not set")
    engine = create_engine(url)
    try:
        await ping(engine)
    finally:
        await engine.dispose()
