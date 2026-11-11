"""Proposal table (FR-PRO-03). Repository methods arrive with the proposal service."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from lakeloom.state.models import Base

__all__ = ["ProposalRow"]


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
