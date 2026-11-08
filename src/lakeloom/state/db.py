"""Async engine factory and liveness ping for the state store (NFR-OPS-01).

PostgreSQL is the dev-stack default; SQLite is the code-level fallback (spec §10).
Schema migrations (Alembic) arrive with the first table in M1 (ADR 0006).
"""

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
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
