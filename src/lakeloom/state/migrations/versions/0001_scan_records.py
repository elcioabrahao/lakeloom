"""scan records (FR-SRC-10)

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("principal_subject", sa.String(255), nullable=False),
        sa.Column("principal_type", sa.String(64), nullable=False),
        sa.Column("client", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_scan_records_source_created_at", "scan_records", ["source", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_scan_records_source_created_at", table_name="scan_records")
    op.drop_table("scan_records")
