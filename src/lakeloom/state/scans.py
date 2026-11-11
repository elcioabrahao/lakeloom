"""Scan record repository (FR-SRC-10).

Scan ids are ``scan_`` + a 26-character Crockford-base32 ULID generated here, so
lexicographic order equals creation order and the pagination cursor (the last id)
is stable.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from lakeloom.errors import LakeloomError
from lakeloom.sources.models import Page, TableDescription, TableProfile, TableRef, TableVersion
from lakeloom.state.ids import new_ulid_id
from lakeloom.state.models import ScanRecordRow

__all__ = ["ScanRecord", "ScanRepository", "ScannedTable", "new_scan_id"]


def new_scan_id(now_ms: int | None = None) -> str:
    """``scan_`` + ULID: 48-bit millisecond timestamp then 80 random bits, 26 chars."""
    return new_ulid_id("scan_", now_ms)


class ScannedTable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ref: TableRef
    version: TableVersion
    om_fqn: str | None = None
    om_id: str | None = None
    description: TableDescription | None = None
    profile: TableProfile | None = None
    error: dict[str, Any] | None = None
    profile_error: dict[str, Any] | None = None


class ScanRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    source: str
    created_at: datetime
    principal_subject: str
    principal_type: str
    client: str | None = None
    status: Literal["completed", "partial", "failed"]
    tables: list[ScannedTable] = []
    warnings: list[str] = []


def _utc(value: datetime) -> datetime:
    """SQLite drops tzinfo; values are stored as UTC, so reattach it."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _to_row(rec: ScanRecord) -> ScanRecordRow:
    payload = rec.model_dump(mode="json", by_alias=True, include={"tables", "warnings"})
    return ScanRecordRow(
        id=rec.id,
        source=rec.source,
        created_at=_utc(rec.created_at),
        principal_subject=rec.principal_subject,
        principal_type=rec.principal_type,
        client=rec.client,
        status=rec.status,
        payload=payload,
    )


def _from_row(row: ScanRecordRow) -> ScanRecord:
    return ScanRecord.model_validate(
        {
            "id": row.id,
            "source": row.source,
            "created_at": _utc(row.created_at),
            "principal_subject": row.principal_subject,
            "principal_type": row.principal_type,
            "client": row.client,
            "status": row.status,
            "tables": row.payload.get("tables", []),
            "warnings": row.payload.get("warnings", []),
        }
    )


class ScanRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(self, record: ScanRecord) -> None:
        async with self._sessions() as session, session.begin():
            session.add(_to_row(record))

    async def get(self, scan_id: str) -> ScanRecord:
        async with self._sessions() as session:
            row = await session.get(ScanRecordRow, scan_id)
        if row is None:
            raise LakeloomError(
                "not_found",
                f"scan {scan_id!r} does not exist",
                "Use a scan_id returned by scan_lake_source (or printed by `lakeloom scan`).",
            )
        return _from_row(row)

    async def latest_for_source(self, source: str) -> ScanRecord | None:
        stmt = (
            select(ScanRecordRow)
            .where(ScanRecordRow.source == source)
            .order_by(ScanRecordRow.id.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _from_row(row) if row else None

    async def list(self, source: str | None, cursor: str | None, limit: int) -> Page[ScanRecord]:
        stmt = select(ScanRecordRow).order_by(ScanRecordRow.id.desc()).limit(limit + 1)
        if source is not None:
            stmt = stmt.where(ScanRecordRow.source == source)
        if cursor is not None:
            stmt = stmt.where(ScanRecordRow.id < cursor)
        async with self._sessions() as session:
            rows = list((await session.execute(stmt)).scalars())
        page, more = rows[:limit], len(rows) > limit
        return Page(
            items=[_from_row(r) for r in page],
            next_cursor=page[-1].id if more and page else None,
        )
