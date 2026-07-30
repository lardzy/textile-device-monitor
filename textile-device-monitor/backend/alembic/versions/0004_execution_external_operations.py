"""add durable execution external-operation fence

Revision ID: 0004_external_operations
Revises: 0003_execution_contract
Create Date: 2026-07-29
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_external_operations"
down_revision: Union[str, Sequence[str], None] = "0003_execution_contract"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)


def upgrade() -> None:
    op.add_column(
        "execution_credentials",
        sa.Column(
            "revision",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.create_table(
        "execution_external_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=False),
        sa.Column("connector_key", sa.String(length=100), nullable=False),
        sa.Column("credential_id", sa.String(length=36), nullable=True),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("account_scope_key", sa.String(length=64), nullable=False),
        sa.Column("remote_business_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("request_summary", JSON_VARIANT, nullable=False),
        sa.Column(
            "preflight_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("approved_by_id", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approval_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("approval_note", sa.Text(), nullable=True),
        sa.Column("fence_token", sa.String(length=36), nullable=True),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("remote_record_id", sa.String(length=200), nullable=True),
        sa.Column("receipt", JSON_VARIANT, nullable=False),
        sa.Column("verification", JSON_VARIANT, nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["execution_users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["execution_credentials.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id"],
            ["execution_node_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["execution_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "node_run_id",
            name="uq_execution_external_operations_node_run",
        ),
        sa.UniqueConstraint(
            "operation_key",
            name="uq_execution_external_operations_key",
        ),
    )
    op.create_index(
        "ix_execution_external_operations_account_scope",
        "execution_external_operations",
        ["account_scope_key"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_approved_by_id",
        "execution_external_operations",
        ["approved_by_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_claim",
        "execution_external_operations",
        ["status", "created_at", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_connector_key",
        "execution_external_operations",
        ["connector_key"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_credential_id",
        "execution_external_operations",
        ["credential_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_lease_expires_at",
        "execution_external_operations",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_node_run_id",
        "execution_external_operations",
        ["node_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_run_created",
        "execution_external_operations",
        ["run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_run_id",
        "execution_external_operations",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_external_operations_status",
        "execution_external_operations",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_execution_external_operations_active_remote_key",
        "execution_external_operations",
        ["remote_business_key"],
        unique=True,
        postgresql_where=sa.text(
            "status IN "
            "('prepared', 'approved', 'in_progress', "
            "'reconciliation_required')"
        ),
        sqlite_where=sa.text(
            "status IN "
            "('prepared', 'approved', 'in_progress', "
            "'reconciliation_required')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_execution_external_operations_active_remote_key",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_status",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_run_id",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_run_created",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_node_run_id",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_lease_expires_at",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_credential_id",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_connector_key",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_claim",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_approved_by_id",
        table_name="execution_external_operations",
    )
    op.drop_index(
        "ix_execution_external_operations_account_scope",
        table_name="execution_external_operations",
    )
    op.drop_table("execution_external_operations")
    op.drop_column("execution_credentials", "revision")
