from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app import db_migrations
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
        assert len({name for name in tables if name.startswith("execution_")}) == 27
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
        assert revision == "0003_execution_contract"
    finally:
        engine.dispose()


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
            version_row = connection.execute(
                text(
                    "SELECT capabilities, contract_checksum "
                    "FROM execution_workflow_versions WHERE id = 'version-1'"
                )
            ).one()
            run_row = connection.execute(
                text(
                    "SELECT capabilities_snapshot, contract_checksum "
                    "FROM execution_runs WHERE id = 'run-1'"
                )
            ).one()
        assert json.loads(version_row.capabilities) == capabilities
        assert json.loads(run_row.capabilities_snapshot) == capabilities
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
