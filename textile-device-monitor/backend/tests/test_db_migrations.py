from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import db_migrations
from app.execution.models import (
    ExecutionCategory,
    ExecutionRun,
    ExecutionUser,
    ExecutionWorkflow,
    ExecutionWorkflowVersion,
)
from app.execution.validation import workflow_contract_checksum


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _legacy_engine(tmp_path, name: str):
    database_url = f"sqlite:///{tmp_path / name}"
    command.upgrade(_config(database_url), db_migrations.LEGACY_BASELINE_REVISION)
    return create_engine(database_url)


def test_test_database_url_requires_explicit_disposable_database(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="explicitly configured"):
        db_migrations.assert_safe_test_database_url()

    with pytest.raises(RuntimeError, match="_test"):
        db_migrations.assert_safe_test_database_url(
            "postgresql://admin:secret@db/textile_monitor"
        )

    assert db_migrations.assert_safe_test_database_url(
        "postgresql://admin:secret@db/textile_monitor_test"
    ).endswith("_test")
    assert (
        db_migrations.assert_safe_test_database_url("sqlite:///:memory:")
        == "sqlite:///:memory:"
    )


def test_empty_database_upgrades_to_execution_head(tmp_path):
    database_path = tmp_path / "fresh_test.db"
    database_url = f"sqlite:///{database_path}"
    config = _config(database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "devices" in tables
        assert "execution_workflows" in tables
        assert "execution_publish_receipts" in tables
        assert "execution_external_operations" in tables
        assert "execution_external_attempts" in tables
        assert "execution_task_snapshot_cache" in tables
        assert "execution_project_rules" in tables
        assert "execution_workflow_releases" in tables
        assert "execution_release_preflights" in tables
        assert "execution_deployment_bindings" in tables
        assert "execution_workflow_activation_receipts" in tables
        assert "execution_worker_node_capabilities" in tables
        assert "execution_human_approval_receipts" in tables
        assert "execution_human_approval_receipt_consumptions" in tables
        assert len({name for name in tables if name.startswith("execution_")}) == 38
        storage_root_columns = {
            column["name"]: column
            for column in inspect(engine).get_columns(
                "execution_storage_roots"
            )
        }
        assert storage_root_columns["binding_revision"]["nullable"] is False
        human_task_columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("execution_human_tasks")
        }
        assert human_task_columns["renderer_contract"]["nullable"] is False
    finally:
        engine.dispose()


def test_existing_baseline_is_preflighted_stamped_and_upgraded(tmp_path):
    database_path = tmp_path / "existing_test.db"
    database_url = f"sqlite:///{database_path}"
    config = _config(database_url)
    command.upgrade(config, db_migrations.LEGACY_BASELINE_REVISION)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))

    with (
        patch.object(db_migrations, "engine", engine),
        patch.object(db_migrations.settings, "DATABASE_URL", database_url),
    ):
        db_migrations.migrate_database()

    try:
        tables = set(inspect(engine).get_table_names())
        assert "execution_runs" in tables
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "0009_execution_v2_primitives"
    finally:
        engine.dispose()


def test_0008_to_0009_preserves_existing_version_run_and_lock_bytes(tmp_path):
    database_path = tmp_path / "execution_v2_history_test.db"
    database_url = f"sqlite:///{database_path}"
    config = _config(database_url)
    command.upgrade(config, "0008_execution_v2_contracts")
    engine = create_engine(database_url)
    definition = {"schema_version": "1.0", "nodes": [], "edges": []}
    dependency_lock = {
        "lock_version": "1.0",
        "node_instances": [],
        "digest": "2" * 64,
    }
    binding = {
        "environment": "test",
        "revision": 3,
        "bindings": {"root_slots": {}},
    }
    asset_lock = {"assets": [], "digest": "3" * 64}
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add_all(
            [
                ExecutionCategory(
                    id="category-p2-history",
                    key="p2-history",
                    name="P2 history",
                ),
                ExecutionUser(
                    id="user-p2-history",
                    username="p2-history",
                    display_name="P2 history",
                    password_hash="unused",
                    role="admin",
                ),
            ]
        )
        db.flush()
        workflow = ExecutionWorkflow(
            id="workflow-p2-history",
            slug="p2-history",
            category_id="category-p2-history",
            name="P2 history",
            draft_definition=definition,
            management_mode="release_v2",
            draft_revision=1,
            published_version_number=1,
            capabilities={"declared": [], "side_effect_level": "none"},
            required_input_count=0,
            created_by_id="user-p2-history",
            updated_by_id="user-p2-history",
        )
        version = ExecutionWorkflowVersion(
            id="version-p2-history",
            workflow_id=workflow.id,
            version_number=1,
            schema_version="1.0",
            definition=definition,
            checksum="4" * 64,
            capabilities={"declared": [], "side_effect_level": "none"},
            contract_checksum="5" * 64,
            contract_format="workflow_release_v2",
            release_digest="6" * 64,
            dependency_lock=dependency_lock,
            dependency_lock_digest="2" * 64,
            deployment_binding_snapshot=binding,
            deployment_binding_digest="7" * 64,
            asset_lock=asset_lock,
            engine_version="2.0.0",
            deployed_contract_checksum="8" * 64,
            published_by_id="user-p2-history",
            published_at=now,
        )
        run = ExecutionRun(
            id="run-p2-history",
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            created_by_id="user-p2-history",
            idempotency_key="p2-history",
            inspection_number="P2-HISTORY",
            mode="test",
            status="completed",
            definition_snapshot=definition,
            definition_checksum="4" * 64,
            capabilities_snapshot={
                "declared": [],
                "side_effect_level": "none",
            },
            contract_checksum="5" * 64,
            contract_format="workflow_release_v2",
            release_digest="6" * 64,
            dependency_lock=dependency_lock,
            dependency_lock_digest="2" * 64,
            deployment_binding_snapshot=binding,
            deployment_binding_digest="7" * 64,
            asset_lock=asset_lock,
            engine_version_snapshot="2.0.0",
            deployed_contract_checksum="8" * 64,
            input_data={},
            global_data={},
            output_data={},
        )
        db.add_all([workflow, version, run])
        db.commit()
    columns = (
        "definition, checksum, contract_checksum, dependency_lock, "
        "dependency_lock_digest, deployment_binding_snapshot, "
        "deployment_binding_digest, asset_lock, engine_version, "
        "deployed_contract_checksum"
    )
    with engine.connect() as connection:
        version_before = tuple(
            connection.execute(
                text(
                    f"SELECT {columns} FROM execution_workflow_versions "
                    "WHERE id = 'version-p2-history'"
                )
            ).one()
        )
        run_before = tuple(
            connection.execute(
                text(
                    "SELECT definition_snapshot, definition_checksum, "
                    "contract_checksum, dependency_lock, dependency_lock_digest, "
                    "deployment_binding_snapshot, deployment_binding_digest, "
                    "asset_lock, engine_version_snapshot, "
                    "deployed_contract_checksum FROM execution_runs "
                    "WHERE id = 'run-p2-history'"
                )
            ).one()
        )
    engine.dispose()

    command.upgrade(config, "0009_execution_v2_primitives")
    upgraded = create_engine(database_url)
    try:
        with upgraded.connect() as connection:
            version_after = tuple(
                connection.execute(
                    text(
                        f"SELECT {columns} FROM execution_workflow_versions "
                        "WHERE id = 'version-p2-history'"
                    )
                ).one()
            )
            run_after = tuple(
                connection.execute(
                    text(
                        "SELECT definition_snapshot, definition_checksum, "
                        "contract_checksum, dependency_lock, "
                        "dependency_lock_digest, deployment_binding_snapshot, "
                        "deployment_binding_digest, asset_lock, "
                        "engine_version_snapshot, deployed_contract_checksum "
                        "FROM execution_runs WHERE id = 'run-p2-history'"
                    )
                ).one()
            )
        assert version_after == version_before
        assert run_after == run_before
    finally:
        upgraded.dispose()


def test_execution_contract_migration_backfills_versions_and_runs(tmp_path):
    database_path = tmp_path / "execution_contract_backfill_test.db"
    database_url = f"sqlite:///{database_path}"
    config = _config(database_url)
    command.upgrade(config, "0002_execution_system")
    engine = create_engine(database_url)
    definition = {
        "schema_version": "1.0",
        "nodes": [],
        "edges": [],
    }
    capabilities = {"read": True, "write": True}
    timestamp = "2026-07-26 00:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO execution_users (
                    id, username, display_name, password_hash, role,
                    is_active, created_at, updated_at
                ) VALUES (
                    'user-1', 'migration-user', 'Migration User',
                    'unused', 'admin', 1, :timestamp, :timestamp
                )
                """
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_categories (
                    id, key, name, sort_order, is_active,
                    created_at, updated_at
                ) VALUES (
                    'category-1', 'migration', 'Migration',
                    0, 1, :timestamp, :timestamp
                )
                """
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_workflows (
                    id, slug, category_id, name, draft_definition,
                    draft_revision, published_version_number, capabilities,
                    required_input_count, is_enabled, created_by_id,
                    updated_by_id, created_at, updated_at
                ) VALUES (
                    'workflow-1', 'migration-workflow', 'category-1',
                    'Migration Workflow', :definition, 1, 1,
                    :capabilities, 0, 1, 'user-1', 'user-1',
                    :timestamp, :timestamp
                )
                """
            ),
            {
                "definition": json.dumps(definition),
                "capabilities": json.dumps(capabilities),
                "timestamp": timestamp,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_workflow_versions (
                    id, workflow_id, version_number, schema_version,
                    definition, checksum, published_by_id, published_at
                ) VALUES (
                    'version-1', 'workflow-1', 1, '1.0',
                    :definition, :checksum, 'user-1', :timestamp
                )
                """
            ),
            {
                "definition": json.dumps(definition),
                "checksum": "1" * 64,
                "timestamp": timestamp,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_runs (
                    id, workflow_id, workflow_version_id, created_by_id,
                    idempotency_key, inspection_number, mode, status,
                    definition_snapshot, definition_checksum, input_data,
                    global_data, output_data, created_at, updated_at
                ) VALUES (
                    'run-1', 'workflow-1', 'version-1', 'user-1',
                    'migration-run', '260001', 'live', 'completed',
                    :definition, :checksum, '{}', '{}', '{}',
                    :timestamp, :timestamp
                )
                """
            ),
            {
                "definition": json.dumps(definition),
                "checksum": "1" * 64,
                "timestamp": timestamp,
            },
        )

    command.upgrade(config, "head")
    expected_contract = workflow_contract_checksum(
        definition,
        capabilities,
    )
    try:
        with engine.connect() as connection:
            workflow_row = connection.execute(
                text(
                    "SELECT management_mode FROM execution_workflows "
                    "WHERE id = 'workflow-1'"
                )
            ).one()
            version_row = connection.execute(
                text(
                    "SELECT capabilities, contract_checksum, release_id, "
                    "dependency_lock, deployed_contract_checksum "
                    "FROM execution_workflow_versions WHERE id = 'version-1'"
                )
            ).one()
            run_row = connection.execute(
                text(
                    "SELECT capabilities_snapshot, contract_checksum, "
                    "release_id, dependency_lock, deployed_contract_checksum "
                    "FROM execution_runs WHERE id = 'run-1'"
                )
            ).one()
        assert json.loads(version_row.capabilities) == capabilities
        assert json.loads(run_row.capabilities_snapshot) == capabilities
        assert workflow_row.management_mode == "draft_v1"
        assert version_row.contract_checksum == expected_contract
        assert run_row.contract_checksum == expected_contract
        version_columns = {
            column["name"]: column
            for column in inspect(engine).get_columns(
                "execution_workflow_versions"
            )
        }
        run_columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("execution_runs")
        }
        assert version_columns["capabilities"]["nullable"] is False
        assert version_columns["contract_checksum"]["nullable"] is False
        assert run_columns["capabilities_snapshot"]["nullable"] is False
        assert run_columns["contract_checksum"]["nullable"] is False
        assert version_row.release_id is None
        assert version_row.dependency_lock is None
        assert version_row.deployed_contract_checksum is None
        assert run_row.release_id is None
        assert run_row.dependency_lock is None
        assert run_row.deployed_contract_checksum is None
    finally:
        engine.dispose()


def test_preflight_rejects_unknown_or_partial_schema(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'unsafe_test.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE devices (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE execution_runs (id VARCHAR(36) PRIMARY KEY)")
        )

    with patch.object(db_migrations, "engine", engine):
        with pytest.raises(RuntimeError) as captured:
            db_migrations.preflight_legacy_schema()

    message = str(captured.value)
    assert "unversioned execution tables" in message
    assert "missing tables" in message
    engine.dispose()


def test_preflight_rejects_missing_legacy_column(tmp_path):
    engine = _legacy_engine(tmp_path, "missing_column_test.db")
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE statistics DROP COLUMN avg_duration"))

    with patch.object(db_migrations, "engine", engine):
        with pytest.raises(
            RuntimeError,
            match="statistics missing columns",
        ) as captured:
            db_migrations.preflight_legacy_schema()

    assert "avg_duration" in str(captured.value)
    engine.dispose()


def test_preflight_rejects_legacy_column_type_drift(tmp_path):
    engine = _legacy_engine(tmp_path, "type_drift_test.db")
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE system_configs RENAME TO system_configs_original")
        )
        connection.execute(
            text(
                """
                CREATE TABLE system_configs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    config_key INTEGER NOT NULL,
                    value_text TEXT,
                    value_json JSON,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(text("DROP TABLE system_configs_original"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX ix_system_configs_config_key "
                "ON system_configs (config_key)"
            )
        )
        connection.execute(
            text("CREATE INDEX ix_system_configs_id ON system_configs (id)")
        )

    with patch.object(db_migrations, "engine", engine):
        with pytest.raises(
            RuntimeError,
            match=r"system_configs\.config_key type",
        ):
            db_migrations.preflight_legacy_schema()

    engine.dispose()


def test_preflight_rejects_missing_named_index(tmp_path):
    engine = _legacy_engine(tmp_path, "missing_index_test.db")
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_devices_id"))

    with patch.object(db_migrations, "engine", engine):
        with pytest.raises(RuntimeError, match="devices indexes") as captured:
            db_migrations.preflight_legacy_schema()

    assert "ix_devices_id" in str(captured.value)
    engine.dispose()


def test_preflight_rejects_foreign_key_constraint_drift(tmp_path):
    engine = _legacy_engine(tmp_path, "foreign_key_drift_test.db")
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE queue_change_logs RENAME TO queue_change_logs_original")
        )
        connection.execute(
            text(
                """
                CREATE TABLE queue_change_logs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    queue_id INTEGER NOT NULL,
                    old_position INTEGER,
                    new_position INTEGER,
                    changed_by VARCHAR(50),
                    changed_by_id VARCHAR(64),
                    change_type VARCHAR(50),
                    remark TEXT,
                    change_time DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(text("DROP TABLE queue_change_logs_original"))
        connection.execute(
            text(
                "CREATE INDEX ix_queue_change_logs_id "
                "ON queue_change_logs (id)"
            )
        )

    with patch.object(db_migrations, "engine", engine):
        with pytest.raises(
            RuntimeError,
            match="queue_change_logs foreign keys",
        ):
            db_migrations.preflight_legacy_schema()

    engine.dispose()


def test_preflight_rejects_server_default_drift(tmp_path):
    engine = _legacy_engine(tmp_path, "default_drift_test.db")
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE system_configs RENAME TO system_configs_original")
        )
        connection.execute(
            text(
                """
                CREATE TABLE system_configs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    config_key VARCHAR(100) NOT NULL,
                    value_text TEXT,
                    value_json JSON,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(text("DROP TABLE system_configs_original"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX ix_system_configs_config_key "
                "ON system_configs (config_key)"
            )
        )
        connection.execute(
            text("CREATE INDEX ix_system_configs_id ON system_configs (id)")
        )

    with patch.object(db_migrations, "engine", engine):
        with pytest.raises(
            RuntimeError,
            match=r"system_configs\.updated_at server default",
        ):
            db_migrations.preflight_legacy_schema()

    engine.dispose()


def test_preflight_rejects_check_constraint_drift(tmp_path):
    engine = _legacy_engine(tmp_path, "check_drift_test.db")
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE system_configs RENAME TO system_configs_original")
        )
        connection.execute(
            text(
                """
                CREATE TABLE system_configs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    config_key VARCHAR(100) NOT NULL
                        CHECK (length(config_key) > 0),
                    value_text TEXT,
                    value_json JSON,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(text("DROP TABLE system_configs_original"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX ix_system_configs_config_key "
                "ON system_configs (config_key)"
            )
        )
        connection.execute(
            text("CREATE INDEX ix_system_configs_id ON system_configs (id)")
        )

    with patch.object(db_migrations, "engine", engine):
        with pytest.raises(
            RuntimeError,
            match="system_configs check constraints",
        ):
            db_migrations.preflight_legacy_schema()

    engine.dispose()
