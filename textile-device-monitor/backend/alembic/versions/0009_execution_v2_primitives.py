"""add Execution v2 primitive human contracts

Revision ID: 0009_execution_v2_primitives
Revises: 0008_execution_v2_contracts
Create Date: 2026-08-21

This revision is deliberately self-contained.  The P1 compatibility Worker
image may carry this migration metadata so its schema-head guard can coexist
with the P2 API without changing any P1 Pack resource bytes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0009_execution_v2_primitives"
down_revision: Union[str, Sequence[str], None] = "0008_execution_v2_contracts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    with op.batch_alter_table("execution_human_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "renderer_contract",
                JSON_VARIANT,
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    op.create_table(
        "execution_human_approval_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=False),
        sa.Column("human_task_id", sa.String(length=36), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.String(length=100), nullable=False),
        sa.Column("subject_digest", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "authorization_snapshot", JSON_VARIANT, nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("receipt_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["execution_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id"],
            ["execution_node_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["human_task_id"],
            ["execution_human_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["execution_users.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "human_task_id",
            "task_revision",
            name="uq_execution_human_approval_receipt_task_revision",
        ),
        sa.UniqueConstraint("receipt_digest"),
    )
    op.create_index(
        "ix_execution_human_approval_receipt_run",
        "execution_human_approval_receipts",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_execution_human_approval_receipt_subject",
        "execution_human_approval_receipts",
        ["subject_type", "subject_digest"],
    )
    op.create_index(
        "ix_execution_human_approval_receipts_node_run_id",
        "execution_human_approval_receipts",
        ["node_run_id"],
    )
    op.create_table(
        "execution_human_approval_receipt_consumptions",
        sa.Column("approval_receipt_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_run_id", sa.String(length=36), nullable=False),
        sa.Column("mutation_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["approval_receipt_id"],
            ["execution_human_approval_receipts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["execution_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["node_run_id"],
            ["execution_node_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("approval_receipt_id"),
    )
    op.create_index(
        "ix_execution_human_approval_receipt_consumption_node_run",
        "execution_human_approval_receipt_consumptions",
        ["node_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_human_approval_receipt_consumption_node_run",
        table_name="execution_human_approval_receipt_consumptions",
    )
    op.drop_table("execution_human_approval_receipt_consumptions")
    op.drop_index(
        "ix_execution_human_approval_receipts_node_run_id",
        table_name="execution_human_approval_receipts",
    )
    op.drop_index(
        "ix_execution_human_approval_receipt_subject",
        table_name="execution_human_approval_receipts",
    )
    op.drop_index(
        "ix_execution_human_approval_receipt_run",
        table_name="execution_human_approval_receipts",
    )
    op.drop_table("execution_human_approval_receipts")
    with op.batch_alter_table("execution_human_tasks") as batch_op:
        batch_op.drop_column("renderer_contract")
