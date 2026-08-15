"""add admin-editable project match rules

Revision ID: 0007_project_rules
Revises: 0006_task_snapshot_cache
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_project_rules"
down_revision: Union[str, Sequence[str], None] = "0006_task_snapshot_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "execution_project_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("rule_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("category_key", sa.String(length=50), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config", JSON_VARIANT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["execution_users.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_key"),
    )
    op.create_index(
        "ix_execution_project_rules_rule_key",
        "execution_project_rules",
        ["rule_key"],
    )
    op.create_index(
        "ix_execution_project_rules_category_key",
        "execution_project_rules",
        ["category_key"],
    )
    # Seed rows are inserted lazily by
    # ``project_rules.ensure_default_project_rules`` so that code constants
    # remain the single seed source for both migrated and test databases.


def downgrade() -> None:
    op.drop_table("execution_project_rules")
