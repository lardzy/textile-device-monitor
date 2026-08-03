"""add read-only legacy task snapshot cache

Revision ID: 0006_task_snapshot_cache
Revises: 0005_external_attempts
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006_task_snapshot_cache"
down_revision: Union[str, Sequence[str], None] = "0005_external_attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "execution_task_snapshot_cache",
        sa.Column("inspection_number", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("snapshot", JSON_VARIANT, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("refresh_requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=100), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("inspection_number"),
    )
    op.create_index(
        "ix_execution_task_snapshot_cache_status",
        "execution_task_snapshot_cache",
        ["status"],
    )
    op.create_index(
        "ix_execution_task_snapshot_cache_refresh_requested_at",
        "execution_task_snapshot_cache",
        ["refresh_requested_at"],
    )
    op.create_index(
        "ix_execution_task_snapshot_cache_expires_at",
        "execution_task_snapshot_cache",
        ["expires_at"],
    )
    op.create_index(
        "ix_execution_task_snapshot_cache_claim_expires_at",
        "execution_task_snapshot_cache",
        ["claim_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("execution_task_snapshot_cache")
