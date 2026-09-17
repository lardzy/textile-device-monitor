"""Archive old workflows and link independently published replacements.

Revision ID: 0010_workflow_replacement
Revises: 0009_execution_v2_primitives
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_workflow_replacement"
down_revision = "0009_execution_v2_primitives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("execution_workflows") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("replaces_workflow_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_execution_workflows_replaces",
            "execution_workflows",
            ["replaces_workflow_id"],
            ["id"],
        )
        batch.create_index(
            "ix_execution_workflows_replaces_workflow_id",
            ["replaces_workflow_id"],
            unique=True,
        )
    op.add_column(
        "execution_workflow_releases",
        sa.Column(
            "migration_source",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        ),
    )


def downgrade() -> None:
    op.drop_column("execution_workflow_releases", "migration_source")
    with op.batch_alter_table("execution_workflows") as batch:
        batch.drop_index("ix_execution_workflows_replaces_workflow_id")
        batch.drop_constraint("fk_execution_workflows_replaces", type_="foreignkey")
        batch.drop_column("replaces_workflow_id")
        batch.drop_column("archived_at")
