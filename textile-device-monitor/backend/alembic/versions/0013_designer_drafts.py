"""Keep editable portable documents separately from published projections."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_designer_drafts"
down_revision = "0012_connector_record_reads"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("execution_workflows", sa.Column("designer_draft", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")))


def downgrade():
    op.drop_column("execution_workflows", "designer_draft")
