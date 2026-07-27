"""freeze execution capabilities and add a durable publish fence

Revision ID: 0003_execution_contract
Revises: 0002_execution_system
Create Date: 2026-07-26
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003_execution_contract"
down_revision: Union[str, Sequence[str], None] = "0002_execution_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()),
    "postgresql",
)


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _contract_checksum(
    definition: dict[str, Any],
    capabilities: dict[str, Any],
) -> str:
    payload = json.dumps(
        {
            "definition": definition,
            "capabilities": capabilities,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def upgrade() -> None:
    op.add_column(
        "execution_workflow_versions",
        sa.Column("capabilities", JSON_VARIANT, nullable=True),
    )
    op.add_column(
        "execution_workflow_versions",
        sa.Column("contract_checksum", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "execution_runs",
        sa.Column("capabilities_snapshot", JSON_VARIANT, nullable=True),
    )
    op.add_column(
        "execution_runs",
        sa.Column("contract_checksum", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "execution_file_mutations",
        sa.Column("publish_fence_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "execution_file_mutations",
        sa.Column(
            "publish_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    workflows = sa.table(
        "execution_workflows",
        sa.column("id", sa.String(length=36)),
        sa.column("capabilities", JSON_VARIANT),
    )
    versions = sa.table(
        "execution_workflow_versions",
        sa.column("id", sa.String(length=36)),
        sa.column("workflow_id", sa.String(length=36)),
        sa.column("definition", JSON_VARIANT),
        sa.column("capabilities", JSON_VARIANT),
        sa.column("contract_checksum", sa.String(length=64)),
    )
    runs = sa.table(
        "execution_runs",
        sa.column("id", sa.String(length=36)),
        sa.column("workflow_id", sa.String(length=36)),
        sa.column("workflow_version_id", sa.String(length=36)),
        sa.column("definition_snapshot", JSON_VARIANT),
        sa.column("capabilities_snapshot", JSON_VARIANT),
        sa.column("contract_checksum", sa.String(length=64)),
    )

    connection = op.get_bind()
    version_rows = connection.execute(
        sa.select(
            versions.c.id,
            versions.c.definition,
            workflows.c.capabilities,
        ).select_from(
            versions.join(
                workflows,
                versions.c.workflow_id == workflows.c.id,
            )
        )
    ).mappings()
    version_contracts: dict[str, tuple[dict[str, Any], str]] = {}
    for row in version_rows:
        definition = _dict(row["definition"])
        capabilities = _dict(row["capabilities"])
        checksum = _contract_checksum(definition, capabilities)
        connection.execute(
            versions.update()
            .where(versions.c.id == row["id"])
            .values(
                capabilities=capabilities,
                contract_checksum=checksum,
            )
        )
        version_contracts[str(row["id"])] = (capabilities, checksum)

    workflow_capabilities = {
        str(row["id"]): _dict(row["capabilities"])
        for row in connection.execute(
            sa.select(workflows.c.id, workflows.c.capabilities)
        ).mappings()
    }
    for row in connection.execute(
        sa.select(
            runs.c.id,
            runs.c.workflow_id,
            runs.c.workflow_version_id,
            runs.c.definition_snapshot,
        )
    ).mappings():
        version_contract = version_contracts.get(
            str(row["workflow_version_id"])
        )
        capabilities = (
            version_contract[0]
            if version_contract is not None
            else workflow_capabilities.get(str(row["workflow_id"]), {})
        )
        checksum = _contract_checksum(
            _dict(row["definition_snapshot"]),
            capabilities,
        )
        connection.execute(
            runs.update()
            .where(runs.c.id == row["id"])
            .values(
                capabilities_snapshot=capabilities,
                contract_checksum=checksum,
            )
        )

    with op.batch_alter_table("execution_workflow_versions") as batch_op:
        batch_op.alter_column(
            "capabilities",
            existing_type=JSON_VARIANT,
            nullable=False,
        )
        batch_op.alter_column(
            "contract_checksum",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.alter_column(
            "capabilities_snapshot",
            existing_type=JSON_VARIANT,
            nullable=False,
        )
        batch_op.alter_column(
            "contract_checksum",
            existing_type=sa.String(length=64),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_file_mutations") as batch_op:
        batch_op.drop_column("publish_started_at")
        batch_op.drop_column("publish_fence_token")
    with op.batch_alter_table("execution_runs") as batch_op:
        batch_op.drop_column("contract_checksum")
        batch_op.drop_column("capabilities_snapshot")
    with op.batch_alter_table("execution_workflow_versions") as batch_op:
        batch_op.drop_column("contract_checksum")
        batch_op.drop_column("capabilities")
