"""Container startup installs the shipped workflows without operator commands."""
import asyncio
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.execution import deploy_builtin_workflows as deployment
from app.execution.models import (
    ExecutionCredential, ExecutionExternalOperation, ExecutionHumanTask,
    ExecutionNodeRun, ExecutionRun, ExecutionUser, ExecutionWorkflow,
    ExecutionWorkflowVersion, ExecutionWorkerHeartbeat,
    ExecutionWorkerNodeCapability, utcnow,
)
from test_execution_builtin_deployment import environment, counts  # noqa: F401


def test_startup_directly_installs_all_workflows_and_removes_old_history(environment):
    db, path, old_id, custom_id, history_id = environment
    old_run = db.get(ExecutionRun, history_id)
    old_run.status = "waiting_human"
    node = ExecutionNodeRun(run_id=history_id, node_id="old-human", node_type="human.input", node_name="Old", status="waiting_human")
    db.add(node)
    db.flush()
    task = ExecutionHumanTask(run_id=history_id, node_run_id=node.id, title="Old pending", status="open")
    operation = ExecutionExternalOperation(operation_key="old-pending", run_id=history_id, connector_key="legacy_fibrecheck", credential_revision=1, account_scope_key="scope", remote_business_key="scope", status="reconciliation_required", payload_checksum="c" * 64, preflight_expires_at=utcnow())
    db.add_all([task, operation])
    db.commit()
    task_id, operation_id = task.id, operation.id
    account_count = db.query(ExecutionUser).count()
    credential_count = db.query(ExecutionCredential).count()

    report = deployment.install_at_startup(db)
    db.expire_all()

    assert report["applied"] is True
    assert {item["slug"] for item in report["workflows"]} == set(deployment.BUILTIN_SLUGS)
    assert all(item["is_enabled"] for item in report["workflows"])
    assert db.get(ExecutionWorkflow, old_id) is None
    assert db.get(ExecutionRun, history_id) is None
    assert db.get(ExecutionHumanTask, task_id) is None
    assert db.get(ExecutionExternalOperation, operation_id) is None
    assert not db.query(ExecutionWorkflowVersion).filter_by(workflow_id=old_id).count()
    assert db.get(ExecutionWorkflow, custom_id).is_enabled
    assert db.query(ExecutionUser).count() == account_count
    assert db.query(ExecutionCredential).count() == credential_count
    assert (path / "electron_microscopy_records" / "keep.bmp").read_bytes() == b"original-image"


def test_startup_selects_configured_admin_without_user_choice(environment):
    db, _path, _old_id, _custom_id, _history_id = environment
    second = ExecutionUser(username="another-admin", display_name="Another", role="admin", password_hash="test")
    db.add(second)
    db.flush()
    db.add(ExecutionCredential(user_id=second.id, system_key="legacy_inspection", account_name="second-test", encrypted_secret="test", is_active=True))
    db.commit()
    report = deployment.install_at_startup(db)
    assert report["operator"] == "operator"
    assert report["applied"] is True


def test_startup_does_not_wait_for_worker_or_claim_worker_readiness(environment):
    db, _path, _old_id, _custom_id, _history_id = environment
    db.query(ExecutionWorkerNodeCapability).delete(synchronize_session=False)
    db.query(ExecutionWorkerHeartbeat).delete(synchronize_session=False)
    db.commit()
    report = deployment.install_at_startup(db)
    assert report["applied"] is True
    assert len(report["workflows"]) == 2
    assert all(item["worker_ready"] is False for item in report["workflows"])
    assert all(item["is_enabled"] for item in report["workflows"])


def test_repeated_startup_keeps_current_runs_and_manual_draft(environment):
    db, _path, _old_id, _custom_id, _history_id = environment
    first = deployment.install_at_startup(db)
    workflow = db.get(ExecutionWorkflow, first["workflows"][0]["workflow_id"])
    version = db.query(ExecutionWorkflowVersion).filter_by(workflow_id=workflow.id, version_number=workflow.published_version_number).one()
    actor = db.query(ExecutionUser).filter_by(username="operator").one()
    run = ExecutionRun(workflow_id=workflow.id, workflow_version_id=version.id, created_by_id=actor.id, idempotency_key="current-run", inspection_number="current-number", status="waiting_human", definition_snapshot=version.definition, definition_checksum=version.checksum, contract_checksum=version.contract_checksum, release_id=version.release_id)
    workflow.designer_draft = {"manual": "keep my unpublished changes"}
    db.add(run)
    db.commit()
    run_id, workflow_id = run.id, workflow.id
    before = counts(db)

    repeated = deployment.install_at_startup(db)
    assert all(item["unchanged"] for item in repeated["workflows"])
    assert counts(db) == before
    assert db.get(ExecutionRun, run_id).status == "waiting_human"
    assert db.get(ExecutionWorkflow, workflow_id).designer_draft == {"manual": "keep my unpublished changes"}
    assert {item["slug"]: item["version"] for item in repeated["workflows"]} == {item["slug"]: item["version"] for item in first["workflows"]}


@pytest.mark.parametrize("environment_name,expected_installs", [("production", 1), ("testing", 0)])
def test_application_startup_installs_after_storage_initialization(monkeypatch, environment_name, expected_installs):
    from app import main

    calls = []
    db = MagicMock()
    db.__enter__.return_value = db
    db.commit.side_effect = lambda: calls.append("commit")
    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(type(settings), "validate_execution_security", lambda self: None)
    monkeypatch.setattr(settings, "APP_ENV", environment_name)
    monkeypatch.setattr(settings, "EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "OCR_ENABLED", False)
    monkeypatch.setattr(settings, "AREA_ENABLED", False)
    monkeypatch.setattr(main, "bootstrap_execution_system", lambda session: calls.append("bootstrap"))
    monkeypatch.setattr(main, "ensure_storage_roots", lambda session: calls.append("storage"))
    def install(session):
        assert session is db
        calls.append("install")
    monkeypatch.setattr(deployment, "install_at_startup", install)
    def close_background_task(coroutine):
        coroutine.close()
    monkeypatch.setattr(main.asyncio, "create_task", close_background_task)

    asyncio.run(main.startup_event())

    assert calls[:3] == ["bootstrap", "storage", "commit"]
    assert calls.count("install") == expected_installs
    if expected_installs:
        assert calls[3] == "install"
