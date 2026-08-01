"""add execution external-attempt bridge claims

Revision ID: 0005_external_attempts
Revises: 0004_external_operations
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_external_attempts"
down_revision: Union[str, Sequence[str], None] = "0004_external_operations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)

_ACTIVE_REMOTE_KEY_WHERE = (
    "status IN "
    "('prepared', 'approved', 'in_progress', "
    "'cancel_pending', 'reconciliation_required')"
)
_PREVIOUS_REMOTE_KEY_WHERE = (
    "status IN "
    "('prepared', 'approved', 'in_progress', "
    "'reconciliation_required')"
)


def upgrade() -> None:
    op.create_table(
        "execution_external_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("bridge_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_stage", sa.String(length=50), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("checkpoints", JSON_VARIANT, nullable=False),
        sa.Column("stdout_summary", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["execution_external_operations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            "attempt_no",
            name="uq_execution_external_attempts_no",
        ),
    )
    op.create_index(
        "ix_execution_external_attempts_lease_expires_at",
        "execution_external_attempts",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_attempts_operation_id",
        "execution_external_attempts",
        ["operation_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_attempts_status",
        "execution_external_attempts",
        ["status"],
        unique=False,
    )
    # The remote business fence must also hold while a claimed operation is
    # being cancelled, so the partial unique index learns cancel_pending.
    op.drop_index(
        "uq_execution_external_operations_active_remote_key",
        table_name="execution_external_operations",
    )
    op.create_index(
        "uq_execution_external_operations_active_remote_key",
        "execution_external_operations",
        ["remote_business_key"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_REMOTE_KEY_WHERE),
        sqlite_where=sa.text(_ACTIVE_REMOTE_KEY_WHERE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_execution_external_operations_active_remote_key",
        table_name="execution_external_operations",
    )
    op.create_index(
        "uq_execution_external_operations_active_remote_key",
        "execution_external_operations",
        ["remote_business_key"],
        unique=True,
        postgresql_where=sa.text(_PREVIOUS_REMOTE_KEY_WHERE),
        sqlite_where=sa.text(_PREVIOUS_REMOTE_KEY_WHERE),
    )
    op.drop_index(
        "ix_execution_external_attempts_status",
        table_name="execution_external_attempts",
    )
    op.drop_index(
        "ix_execution_external_attempts_operation_id",
        table_name="execution_external_attempts",
    )
    op.drop_index(
        "ix_execution_external_attempts_lease_expires_at",
        table_name="execution_external_attempts",
    )
    op.drop_table("execution_external_attempts")
