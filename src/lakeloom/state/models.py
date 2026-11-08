"""SQLAlchemy ORM rows for the state store (FR-SRC-10)."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["Base", "ScanRecordRow"]


class Base(DeclarativeBase):
    pass


class ScanRecordRow(Base):
    __tablename__ = "scan_records"
    __table_args__ = (Index("ix_scan_records_source_created_at", "source", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    principal_subject: Mapped[str] = mapped_column(String(255))
    principal_type: Mapped[str] = mapped_column(String(64))
    client: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
