"""Audit log repository (NFR-SEC-12). Rows are append-only."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from lakeloom.sources.models import Page
from lakeloom.state.ids import new_ulid_id
from lakeloom.state.models import AuditLogRow

if TYPE_CHECKING:
    from lakeloom.core.audit import AuditRecord

__all__ = ["AuditRepository"]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AuditRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(self, record: "AuditRecord") -> None:
        at = _utc(record.at)
        # The id embeds the record time, so id order is time order and the cursor is the id.
        row = AuditLogRow(
            id=new_ulid_id("aud_", int(at.timestamp() * 1000)),
            at=at,
            principal_subject=record.principal_subject,
            principal_type=record.principal_type,
            client=record.client,
            tool=record.tool,
            model=record.model,
            proposal_id=record.proposal_id,
            change_set_id=record.change_set_id,
            result=record.result,
            error_code=record.error_code,
        )
        async with self._sessions() as session, session.begin():
            session.add(row)

    async def list(
        self, since: datetime | None, cursor: str | None, limit: int
    ) -> "Page[AuditRecord]":
        """Records oldest first from ``since``; ``next_cursor`` is the last id of a full page."""
        from lakeloom.core.audit import AuditRecord

        stmt = select(AuditLogRow).order_by(AuditLogRow.id).limit(limit + 1)
        if since is not None:
            stmt = stmt.where(AuditLogRow.at >= _utc(since))
        if cursor is not None:
            stmt = stmt.where(AuditLogRow.id > cursor)
        async with self._sessions() as session:
            rows = list((await session.execute(stmt)).scalars())
        page, more = rows[:limit], len(rows) > limit
        items = [
            AuditRecord.model_validate(
                {c: getattr(r, c) for c in AuditRecord.model_fields} | {"at": _utc(r.at)}
            )
            for r in page
        ]
        return Page(items=items, next_cursor=page[-1].id if more and page else None)
