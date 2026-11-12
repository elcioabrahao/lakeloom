"""Proposal repository (FR-PRO-03).

One row per proposal. The full :class:`~lakeloom.core.proposals.Proposal` (parts, operations,
provenance, matches, warnings) is the JSON ``payload``; the columns repeat what is filtered
on (model, status) or audited (principal, client, change set ids). Updates are
compare-and-set on the proposal's ``revision`` so two writers can never silently overwrite
each other.
"""

import base64
import binascii
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Index, String, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from lakeloom.errors import LakeloomError
from lakeloom.state.models import Base

if TYPE_CHECKING:
    from lakeloom.core.proposals import Proposal

__all__ = [
    "OPEN_STATUSES",
    "PENDING_STATUSES",
    "ProposalRepository",
    "ProposalRow",
    "decode_cursor",
    "encode_cursor",
]

# Proposals whose operations are still pending in OM (dedupe treats their ops as proposed)
# and that can still be discarded: a review asked for changes, or an apply stopped part-way.
OPEN_STATUSES = ("draft", "submitted", "changes_requested", "apply_failed")
# awaiting a reviewer: submitted, or an approval that stopped part-way
PENDING_STATUSES = ("submitted", "apply_failed")
_WIDTHS = {"principal_subject": 255, "principal_type": 64, "client": 255}


class ProposalRow(Base):
    __tablename__ = "proposals"
    __table_args__ = (Index("ix_proposals_model_status", "model", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    change_set_ids: Mapped[Any] = mapped_column(JSON)
    scan_id: Mapped[str | None] = mapped_column(String(64))
    principal_subject: Mapped[str] = mapped_column(String(255))
    principal_type: Mapped[str] = mapped_column(String(64))
    client: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


def _utc(value: datetime) -> datetime:
    """SQLite drops tzinfo; values are stored as UTC, so reattach it."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fit(value: str | None, column: str) -> str | None:
    return None if value is None else value.replace("\x00", "")[: _WIDTHS[column]]


def _fill(row: ProposalRow, proposal: "Proposal") -> None:
    row.model = proposal.model
    row.status = proposal.status
    row.change_set_ids = proposal.change_set_ids()
    row.scan_id = proposal.scan_id
    row.principal_subject = _fit(proposal.provenance.principal_subject, "principal_subject") or ""
    row.principal_type = _fit(proposal.provenance.principal_type, "principal_type") or ""
    row.client = _fit(proposal.provenance.client, "client")
    row.created_at = _utc(proposal.created_at)
    row.updated_at = _utc(proposal.updated_at)
    row.payload = proposal.model_dump(mode="json")


def _from_row(row: ProposalRow) -> "Proposal":
    from lakeloom.core.proposals import Proposal

    return Proposal.model_validate(row.payload)


def encode_cursor(proposal_id: str) -> str:
    return base64.urlsafe_b64encode(proposal_id.encode()).decode()


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        value = raw.decode()
        if base64.urlsafe_b64encode(raw).decode() != cursor or not value:
            raise ValueError(cursor)
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise LakeloomError(
            "validation_error",
            "Invalid cursor",
            "pass the next_cursor from a previous list_pending_reviews call, or omit it",
        ) from exc
    return value


def _not_found(proposal_id: str) -> LakeloomError:
    return LakeloomError(
        "not_found",
        f"proposal {proposal_id!r} does not exist",
        "Use a proposal_id returned by propose_from_scan or propose_realization.",
    )


class ProposalRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(self, proposal: "Proposal") -> None:
        row = ProposalRow(id=proposal.id)
        _fill(row, proposal)
        async with self._sessions() as session, session.begin():
            session.add(row)

    async def get(self, proposal_id: str) -> "Proposal":
        async with self._sessions() as session:
            row = await session.get(ProposalRow, proposal_id)
        if row is None:
            raise _not_found(proposal_id)
        return _from_row(row)

    async def update(self, proposal: "Proposal", *, expected_revision: int) -> None:
        """Store ``proposal`` if the stored one is still at ``expected_revision``.

        ``conflict`` when another writer got there first (nothing is written then).
        """
        async with self._sessions() as session, session.begin():
            stmt = select(ProposalRow).where(ProposalRow.id == proposal.id).with_for_update()
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise _not_found(proposal.id)
            stored = row.payload.get("revision", 0)
            if stored != expected_revision:
                raise LakeloomError(
                    "conflict",
                    f"proposal {proposal.id!r} was changed concurrently",
                    "Call get_proposal to see its current state, then retry.",
                )
            _fill(row, proposal)

    async def delete(self, proposal_id: str) -> None:
        """Remove a proposal that never got an OM change set (failed creation)."""
        async with self._sessions() as session, session.begin():
            row = await session.get(ProposalRow, proposal_id)
            if row is not None:
                await session.delete(row)

    async def open_for_model(self, model: str) -> "list[Proposal]":
        """Open proposals of ``model`` (:data:`OPEN_STATUSES`), oldest first."""
        stmt = (
            select(ProposalRow)
            .where(ProposalRow.model == model, ProposalRow.status.in_(OPEN_STATUSES))
            .order_by(ProposalRow.id)
        )
        async with self._sessions() as session:
            rows = list((await session.execute(stmt)).scalars())
        return [_from_row(r) for r in rows]

    async def list_by_status(
        self, statuses: Sequence[str], model: str | None, cursor: str | None, limit: int
    ) -> "tuple[list[Proposal], str | None]":
        """Proposals in ``statuses`` (of ``model`` when given), oldest first (ids are ULIDs).

        ``cursor`` is the opaque ``next_cursor`` of a previous call (``validation_error`` when
        it is not one). Returns up to ``limit`` proposals and the cursor of the next page, or
        ``None`` on the last page.
        """
        after = decode_cursor(cursor)
        stmt = select(ProposalRow).where(ProposalRow.status.in_(list(statuses)))
        if model is not None:
            stmt = stmt.where(ProposalRow.model == model)
        if after is not None:
            stmt = stmt.where(ProposalRow.id > after)
        stmt = stmt.order_by(ProposalRow.id).limit(limit + 1)
        async with self._sessions() as session:
            rows = list((await session.execute(stmt)).scalars())
        page = rows[:limit]
        more = len(rows) > limit
        return [_from_row(r) for r in page], encode_cursor(page[-1].id) if more else None

    async def pending_op_keys(self, model: str) -> dict[str, str]:
        """``op_key`` -> proposal id of every operation in an open proposal of ``model``
        (the ``pending`` callable of :func:`lakeloom.core.dedupe.dedupe`)."""
        from lakeloom.core.operations import op_key

        out: dict[str, str] = {}
        for proposal in await self.open_for_model(model):
            for part in proposal.parts:
                for op in part.operations:
                    out.setdefault(op_key(op), proposal.id)
        return out
