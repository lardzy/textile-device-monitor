"""add Execution v2 release contracts and exact worker capabilities

Revision ID: 0008_execution_v2_contracts
Revises: 0007_project_rules
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0008_execution_v2_contracts"
down_revision: Union[str, Sequence[str], None] = "0007_project_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "execution_workflow_releases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=True),
        sa.Column("source_slug", sa.String(length=100), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("release_digest", sa.String(length=64), nullable=False),
        sa.Column("format_version", sa.String(length=20), nullable=False),
        sa.Column("portable_document", JSON_VARIANT, nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["execution_workflows.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["execution_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_slug",
            "source_version",
            name="uq_execution_workflow_release_source_identity",
        ),
    )
    op.create_index(
        "ix_execution_workflow_release_status_created",
        "execution_workflow_releases",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_execution_workflow_releases_source_slug",
        "execution_workflow_releases",
        ["source_slug"],
    )
    op.create_index(
        "ix_execution_workflow_releases_release_digest",
        "execution_workflow_releases",
        ["release_digest"],
    )
    op.create_index(
        "ix_execution_workflow_releases_status",
        "execution_workflow_releases",
        ["status"],
    )
    op.create_index(
        "ix_execution_workflow_releases_workflow_id",
        "execution_workflow_releases",
        ["workflow_id"],
    )

    op.create_table(
        "execution_release_preflights",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("release_digest", sa.String(length=64), nullable=False),
        sa.Column("registry_revision", sa.String(length=64), nullable=False),
        sa.Column("binding_revision", sa.Integer(), nullable=True),
        sa.Column("report", JSON_VARIANT, nullable=False),
        sa.Column("portable_document", JSON_VARIANT, nullable=True),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("consumed_by_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["execution_workflow_releases.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["execution_users.id"]),
        sa.ForeignKeyConstraint(["consumed_by_id"], ["execution_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_execution_release_preflight_expiry",
        "execution_release_preflights",
        ["expires_at", "consumed_at"],
    )
    op.create_index(
        "ix_execution_release_preflights_release_id",
        "execution_release_preflights",
        ["release_id"],
    )
    op.create_index(
        "ix_execution_release_preflights_token_hash",
        "execution_release_preflights",
        ["token_hash"],
    )
    op.create_index(
        "ix_execution_release_preflights_release_digest",
        "execution_release_preflights",
        ["release_digest"],
    )
    op.create_index(
        "ix_execution_release_preflights_expires_at",
        "execution_release_preflights",
        ["expires_at"],
    )

    op.create_table(
        "execution_deployment_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=False),
        sa.Column("environment", sa.String(length=100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("binding", JSON_VARIANT, nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["execution_workflow_releases.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["execution_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id",
            "environment",
            "revision",
            name="uq_execution_deployment_binding_revision",
        ),
    )
    op.create_index(
        "ix_execution_deployment_binding_current",
        "execution_deployment_bindings",
        ["release_id", "environment", "revision"],
    )
    op.create_index(
        "ix_execution_deployment_bindings_release_id",
        "execution_deployment_bindings",
        ["release_id"],
    )
    op.create_index(
        "ix_execution_deployment_bindings_digest",
        "execution_deployment_bindings",
        ["digest"],
    )

    op.create_table(
        "execution_workflow_activation_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("from_version_number", sa.Integer(), nullable=True),
        sa.Column("to_version_number", sa.Integer(), nullable=False),
        sa.Column("release_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "deployment_binding_digest", sa.String(length=64), nullable=True
        ),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["execution_workflows.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["execution_workflow_releases.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["execution_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_execution_workflow_activation_receipt_workflow",
        "execution_workflow_activation_receipts",
        ["workflow_id", "created_at"],
    )
    op.create_index(
        "ix_execution_workflow_activation_receipts_workflow_id",
        "execution_workflow_activation_receipts",
        ["workflow_id"],
    )
    op.create_index(
        "ix_execution_workflow_activation_receipts_release_id",
        "execution_workflow_activation_receipts",
        ["release_id"],
    )

    op.create_table(
        "execution_worker_node_capabilities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column(
            "execution_binding_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("node_type", sa.String(length=160), nullable=False),
        sa.Column("node_type_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "implementation_digest", sa.String(length=64), nullable=False
        ),
        sa.Column("pack_id", sa.String(length=100), nullable=False),
        sa.Column("pack_version", sa.String(length=50), nullable=False),
        sa.Column("execution_kind", sa.String(length=30), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["execution_worker_heartbeats.worker_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "worker_id",
            "execution_binding_digest",
            name="uq_execution_worker_node_capability_binding",
        ),
    )
    op.create_index(
        "ix_execution_worker_node_capability_claim",
        "execution_worker_node_capabilities",
        ["execution_binding_digest", "ready"],
    )
    op.create_index(
        "ix_execution_worker_node_capabilities_worker_id",
        "execution_worker_node_capabilities",
        ["worker_id"],
    )
    op.create_index(
        "ix_execution_worker_node_capabilities_ready",
        "execution_worker_node_capabilities",
        ["ready"],
    )

    op.add_column(
        "execution_workflows",
        sa.Column(
            "management_mode",
            sa.String(length=20),
            nullable=False,
            server_default="draft_v1",
        ),
    )
    op.create_index(
        "ix_execution_workflows_management_mode",
        "execution_workflows",
        ["management_mode"],
    )
    op.add_column(
        "execution_storage_roots",
        sa.Column(
            "binding_revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    _add_contract_snapshot_columns("execution_workflow_versions", run=False)
    _add_contract_snapshot_columns("execution_runs", run=True)

    op.add_column(
        "execution_node_runs",
        sa.Column("execution_kind", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "execution_node_runs",
        sa.Column(
            "execution_binding_digest", sa.String(length=64), nullable=True
        ),
    )
    op.create_index(
        "ix_execution_node_runs_execution_binding_digest",
        "execution_node_runs",
        ["execution_binding_digest"],
    )
    op.add_column(
        "execution_node_attempts",
        sa.Column("execution_kind", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "execution_node_attempts",
        sa.Column(
            "execution_binding_digest", sa.String(length=64), nullable=True
        ),
    )
    op.create_index(
        "ix_execution_node_attempts_execution_binding_digest",
        "execution_node_attempts",
        ["execution_binding_digest"],
    )
    op.add_column(
        "execution_worker_heartbeats",
        sa.Column("protocol_version", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "execution_worker_heartbeats",
        sa.Column("engine_version", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "execution_worker_heartbeats",
        sa.Column("capability_digest", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_execution_worker_heartbeats_capability_digest",
        "execution_worker_heartbeats",
        ["capability_digest"],
    )


def _add_contract_snapshot_columns(table_name: str, *, run: bool) -> None:
    # SQLite cannot add a foreign-key constraint with a plain ALTER COLUMN.
    # Alembic batch mode rebuilds the table there while emitting ordinary
    # ALTER statements on PostgreSQL, preserving the same FK contract.
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(
            sa.Column("contract_format", sa.String(length=50))
        )
        batch_op.add_column(sa.Column("release_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            f"fk_{table_name}_release_id",
            "execution_workflow_releases",
            ["release_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.add_column(sa.Column("release_digest", sa.String(64)))
        batch_op.add_column(sa.Column("dependency_lock", JSON_VARIANT))
        batch_op.add_column(
            sa.Column("dependency_lock_digest", sa.String(64))
        )
        batch_op.add_column(
            sa.Column("deployment_binding_snapshot", JSON_VARIANT)
        )
        batch_op.add_column(
            sa.Column("deployment_binding_digest", sa.String(64))
        )
        batch_op.add_column(sa.Column("asset_lock", JSON_VARIANT))
        batch_op.add_column(
            sa.Column(
                "engine_version_snapshot" if run else "engine_version",
                sa.String(50),
            )
        )
        batch_op.add_column(
            sa.Column("deployed_contract_checksum", sa.String(64))
        )
    op.create_index(
        f"ix_{table_name}_release_id", table_name, ["release_id"]
    )
    op.create_index(
        f"ix_{table_name}_release_digest", table_name, ["release_digest"]
    )
    op.create_index(
        f"ix_{table_name}_deployed_contract_checksum",
        table_name,
        ["deployed_contract_checksum"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_worker_heartbeats_capability_digest",
        table_name="execution_worker_heartbeats",
    )
    op.drop_column("execution_worker_heartbeats", "capability_digest")
    op.drop_column("execution_worker_heartbeats", "engine_version")
    op.drop_column("execution_worker_heartbeats", "protocol_version")
    op.drop_index(
        "ix_execution_node_attempts_execution_binding_digest",
        table_name="execution_node_attempts",
    )
    op.drop_column("execution_node_attempts", "execution_binding_digest")
    op.drop_column("execution_node_attempts", "execution_kind")
    op.drop_index(
        "ix_execution_node_runs_execution_binding_digest",
        table_name="execution_node_runs",
    )
    op.drop_column("execution_node_runs", "execution_binding_digest")
    op.drop_column("execution_node_runs", "execution_kind")

    _drop_contract_snapshot_columns("execution_runs", run=True)
    _drop_contract_snapshot_columns("execution_workflow_versions", run=False)
    with op.batch_alter_table("execution_storage_roots") as batch_op:
        batch_op.drop_column("binding_revision")
    op.drop_index(
        "ix_execution_workflows_management_mode",
        table_name="execution_workflows",
    )
    op.drop_column("execution_workflows", "management_mode")

    op.drop_table("execution_worker_node_capabilities")
    op.drop_table("execution_workflow_activation_receipts")
    op.drop_table("execution_deployment_bindings")
    op.drop_table("execution_release_preflights")
    op.drop_table("execution_workflow_releases")


def _drop_contract_snapshot_columns(table_name: str, *, run: bool) -> None:
    op.drop_index(
        f"ix_{table_name}_deployed_contract_checksum", table_name=table_name
    )
    op.drop_index(f"ix_{table_name}_release_digest", table_name=table_name)
    op.drop_index(f"ix_{table_name}_release_id", table_name=table_name)
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_constraint(
            f"fk_{table_name}_release_id", type_="foreignkey"
        )
        batch_op.drop_column("deployed_contract_checksum")
        batch_op.drop_column(
            "engine_version_snapshot" if run else "engine_version"
        )
        batch_op.drop_column("asset_lock")
        batch_op.drop_column("deployment_binding_digest")
        batch_op.drop_column("deployment_binding_snapshot")
        batch_op.drop_column("dependency_lock_digest")
        batch_op.drop_column("dependency_lock")
        batch_op.drop_column("release_digest")
        batch_op.drop_column("release_id")
        batch_op.drop_column("contract_format")
