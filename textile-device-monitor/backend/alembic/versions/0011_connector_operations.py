"""Let the existing external-operation queue accept requests without a Run.

Revision ID: 0011_connector_operations
Revises: 0010_workflow_replacement
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_connector_operations"
down_revision = "0010_workflow_replacement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("execution_external_operations") as batch:
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("node_run_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("created_by_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_external_operations_creator", "execution_users", ["created_by_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_execution_external_operations_created_by_id", ["created_by_id"])
    op.execute(sa.text(
        "UPDATE execution_external_operations SET created_by_id = "
        "(SELECT created_by_id FROM execution_runs WHERE id = execution_external_operations.run_id)"
    ))


def downgrade() -> None:
    # A downgrade must never silently discard standalone operation history.
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM execution_external_operations WHERE run_id IS NULL"
    )).scalar_one()
    if count:
        raise RuntimeError("Standalone operation history requires migration 0011")
    with op.batch_alter_table("execution_external_operations") as batch:
        batch.drop_index("ix_execution_external_operations_created_by_id")
        batch.drop_constraint("fk_external_operations_creator", type_="foreignkey")
        batch.drop_column("created_by_id")
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("node_run_id", existing_type=sa.String(36), nullable=False)
