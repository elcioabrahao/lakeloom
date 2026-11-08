"""Async engine factory and liveness ping for the state store (NFR-OPS-01).

PostgreSQL is the dev-stack default; SQLite is the code-level fallback (spec §10).
Schema migrations are Alembic scripts packaged in ``lakeloom/state/migrations`` (ADR 0006);
``upgrade`` applies them programmatically, so no alembic.ini is needed at runtime.
"""

from importlib.resources import as_file, files
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_ASYNC_SCHEMES = {
    "sqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
}


def to_async_url(url: str) -> str:
    """Map a sync-style URL to its async driver; already-async URLs are unchanged."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return f"{_ASYNC_SCHEMES.get(scheme, scheme)}://{rest}"


def create_engine(url: str) -> AsyncEngine:
    """Create the async engine; for SQLite, create the parent directory if missing."""
    async_url = to_async_url(url)
    parsed = make_url(async_url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database or "").parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(async_url, pool_pre_ping=True)


async def ping(engine: AsyncEngine) -> None:
    """Run ``SELECT 1``; raises if the database is unreachable."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


_UPGRADE_LOCK_KEY = 0x4C4B4C4D  # arbitrary constant: serialises replicas starting together


def _run_upgrade(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        # Transaction-scoped: released at commit; a second replica waits, then sees head.
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _UPGRADE_LOCK_KEY})
    with as_file(files("lakeloom.state") / "migrations") as script_dir:
        cfg = Config()
        cfg.set_main_option("script_location", str(script_dir))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


async def upgrade(engine: AsyncEngine) -> None:
    """Apply all migrations (Alembic ``upgrade head``); idempotent, SQLite and PostgreSQL."""
    async with engine.begin() as conn:
        await conn.run_sync(_run_upgrade)
