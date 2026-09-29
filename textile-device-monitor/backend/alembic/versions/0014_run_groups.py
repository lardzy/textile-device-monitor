"""Associate sequential groups with their existing parent Run."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_run_groups"
down_revision = "0013_designer_drafts"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("execution_runs") as batch:
        batch.add_column(sa.Column("parent_run_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("batch_context", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))
        batch.create_foreign_key("fk_execution_runs_parent", "execution_runs", ["parent_run_id"], ["id"])
        batch.create_index("ix_execution_runs_parent_run_id", ["parent_run_id"])


def downgrade():
    with op.batch_alter_table("execution_runs") as batch:
        batch.drop_index("ix_execution_runs_parent_run_id")
        batch.drop_constraint("fk_execution_runs_parent", type_="foreignkey")
        batch.drop_column("batch_context")
        batch.drop_column("parent_run_id")
