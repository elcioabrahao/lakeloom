"""proposals and audit log (FR-PRO-03, NFR-SEC-12)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proposals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("change_set_ids", sa.JSON(), nullable=False),
        sa.Column("scan_id", sa.String(64), nullable=True),
        sa.Column("principal_subject", sa.String(255), nullable=False),
        sa.Column("principal_type", sa.String(64), nullable=False),
        sa.Column("client", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_proposals_model_status", "proposals", ["model", "status"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("principal_subject", sa.String(255), nullable=False),
        sa.Column("principal_type", sa.String(64), nullable=False),
        sa.Column("client", sa.String(255), nullable=True),
        sa.Column("tool", sa.String(128), nullable=False),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("proposal_id", sa.String(64), nullable=True),
        sa.Column("change_set_id", sa.String(255), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_proposals_model_status", table_name="proposals")
    op.drop_table("proposals")
