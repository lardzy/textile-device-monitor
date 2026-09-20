"""Reuse the read-only snapshot queue for opt-in record details.

Revision ID: 0012_connector_record_reads
Revises: 0011_connector_operations
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_connector_record_reads"
down_revision = "0011_connector_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_task_snapshot_cache", sa.Column(
        "include_check_records", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column("execution_task_snapshot_cache", sa.Column("check_records", sa.JSON()))


def downgrade() -> None:
    # These are disposable read caches; operation and workflow history are untouched.
    with op.batch_alter_table("execution_task_snapshot_cache") as batch:
        batch.drop_column("check_records")
        batch.drop_column("include_check_records")
