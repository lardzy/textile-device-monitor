"""Foreign-key regression for the explicitly requested legacy removal."""
from datetime import datetime, timezone
from itertools import count
import os

import pytest
from sqlalchemy import Boolean, DateTime, Integer, JSON, create_engine, event, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.execution import models as m
from app.execution.retire_legacy_workflows import LEGACY_SLUGS, purge_legacy_workflows


@pytest.fixture
def environment(tmp_path):
    postgres = os.environ.get("EXECUTION_DEPLOYMENT_TEST_POSTGRES") == "1"
    engine = create_engine(os.environ["TEST_DATABASE_URL"] if postgres else f"sqlite:///{tmp_path / 'legacy_purge_test.db'}")
    if postgres:
        assert engine.dialect.name == "postgresql" and engine.url.database.endswith("_test")
        Base.metadata.drop_all(engine)
    else:
        @event.listens_for(engine, "connect")
        def enforce_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    sequence = count(1)
    with Session(engine) as db:
        def row(model, **values):
            serial = next(sequence)
            for column in model.__table__.columns:
                if column.name in values or column.nullable or column.default is not None or column.server_default is not None:
                    continue
                if column.primary_key and isinstance(column.type, Integer):
                    continue
                if isinstance(column.type, DateTime):
                    values[column.name] = datetime.now(timezone.utc)
                elif isinstance(column.type, Boolean):
                    values[column.name] = True
                elif isinstance(column.type, Integer):
                    values[column.name] = serial
                elif isinstance(column.type, JSON):
                    values[column.name] = {}
                else:
                    values[column.name] = f"value-{serial}"
            obj = model(**values)
            db.add(obj)
            db.flush()
            return obj
        yield db, row, tmp_path
    Base.metadata.drop_all(engine)
    if postgres:
        Base.metadata.create_all(engine)
    engine.dispose()


def seed(db, row):
    user = row(m.ExecutionUser, username="operator", role="admin")
    category = row(m.ExecutionCategory)
    credential = row(m.ExecutionCredential, user_id=user.id)
    root = row(m.ExecutionStorageRoot)
    old = row(m.ExecutionWorkflow, slug="electron-cross-section-gbt36422", category_id=category.id)
    old_empty = row(m.ExecutionWorkflow, slug="electron-source-selection", category_id=category.id)
    custom = row(m.ExecutionWorkflow, slug="custom-kept", category_id=category.id)
    current = row(m.ExecutionWorkflow, slug="fiber-microscopy-v2", category_id=category.id, management_mode="release_v2")
    release = row(m.ExecutionWorkflowRelease, workflow_id=old.id, source_slug=old.slug, created_by_id=user.id)
    version = row(m.ExecutionWorkflowVersion, workflow_id=old.id, release_id=release.id)
    row(m.ExecutionDeploymentBinding, release_id=release.id, created_by_id=user.id)
    row(m.ExecutionReleasePreflight, release_id=release.id, created_by_id=user.id)
    row(m.ExecutionReleasePreflight, portable_document={"release": {"slug": old.slug}}, created_by_id=user.id)
    row(m.ExecutionWorkflowActivationReceipt, workflow_id=old.id, release_id=release.id, actor_user_id=user.id)
    run = row(m.ExecutionRun, workflow_id=old.id, workflow_version_id=version.id, created_by_id=user.id, status="running")
    child = row(m.ExecutionRun, workflow_id=current.id, created_by_id=user.id, parent_run_id=run.id, status="waiting_external")
    row(m.ExecutionRun, workflow_id=current.id, created_by_id=user.id, parent_run_id=child.id, status="completed")
    kept_run = row(m.ExecutionRun, workflow_id=custom.id, created_by_id=user.id, status="completed")
    node = row(m.ExecutionNodeRun, run_id=run.id)
    row(m.ExecutionNodeAttempt, node_run_id=node.id)
    row(m.ExecutionEdgeRun, run_id=run.id)
    task = row(m.ExecutionHumanTask, run_id=run.id, node_run_id=node.id)
    approval = row(m.ExecutionHumanApprovalReceipt, run_id=run.id, node_run_id=node.id, human_task_id=task.id, actor_user_id=user.id)
    row(m.ExecutionHumanApprovalReceiptConsumption, run_id=run.id, node_run_id=node.id, approval_receipt_id=approval.id)
    operation = row(m.ExecutionExternalOperation, run_id=run.id, node_run_id=node.id, created_by_id=user.id, status="in_progress", credential_id=credential.id)
    row(m.ExecutionExternalAttempt, operation_id=operation.id)
    artifact = row(m.ExecutionArtifact, run_id=run.id, node_run_id=node.id, storage_root_id=root.id)
    another_artifact = row(m.ExecutionArtifact, run_id=child.id, storage_root_id=root.id)
    row(m.ExecutionArtifactRelation, parent_artifact_id=artifact.id, child_artifact_id=another_artifact.id)
    mutation = row(m.ExecutionFileMutation, run_id=run.id, node_run_id=node.id, source_artifact_id=artifact.id, working_artifact_id=another_artifact.id)
    row(m.ExecutionPublishReceipt, mutation_id=mutation.id, artifact_id=artifact.id, target_storage_root_id=root.id)
    row(m.ExecutionEvent, run_id=run.id)
    row(m.ExecutionOutbox, aggregate_id=run.id)
    row(m.ExecutionAuditLog, resource_id=operation.id, actor_user_id=user.id)
    kept_event = row(m.ExecutionEvent, run_id=kept_run.id)
    kept_audit = row(m.ExecutionAuditLog, resource_id=kept_run.id)
    db.commit()
    return {"old": old.id, "old_empty": old_empty.id, "custom": custom.id, "current": current.id,
            "kept_run": kept_run.id, "kept_event": kept_event.id, "kept_audit": kept_audit.id,
            "user": user.id, "credential": credential.id, "root": root.id}


def counts(db):
    return {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}


def test_removes_old_records_including_active_children_but_keeps_current_and_files(environment):
    db, row, directory = environment
    identities = seed(db, row)
    source = directory / "original.xls"
    source.write_bytes(b"do not delete source files")
    result = purge_legacy_workflows(db)
    db.commit()
    assert {item["slug"] for item in result["removed"]} == {"electron-cross-section-gbt36422", "electron-source-selection"}
    assert db.query(m.ExecutionWorkflow).count() == 2
    assert db.query(m.ExecutionWorkflow).filter(m.ExecutionWorkflow.slug.in_(LEGACY_SLUGS)).count() == 0
    assert db.query(m.ExecutionWorkflowVersion).count() == 0
    assert db.query(m.ExecutionWorkflowRelease).count() == 0
    assert db.query(m.ExecutionRun).count() == 1
    assert db.get(m.ExecutionRun, identities["kept_run"]) is not None
    for model in (m.ExecutionNodeRun, m.ExecutionNodeAttempt, m.ExecutionEdgeRun,
                  m.ExecutionHumanTask, m.ExecutionHumanApprovalReceipt,
                  m.ExecutionHumanApprovalReceiptConsumption, m.ExecutionExternalOperation,
                  m.ExecutionExternalAttempt, m.ExecutionArtifact, m.ExecutionArtifactRelation,
                  m.ExecutionFileMutation, m.ExecutionPublishReceipt, m.ExecutionOutbox,
                  m.ExecutionDeploymentBinding, m.ExecutionReleasePreflight,
                  m.ExecutionWorkflowActivationReceipt):
        assert db.query(model).count() == 0, model.__name__
    assert db.get(m.ExecutionEvent, identities["kept_event"]) is not None
    assert db.get(m.ExecutionAuditLog, identities["kept_audit"]) is not None
    assert db.query(m.ExecutionAuditLog).count() == 1
    assert db.get(m.ExecutionUser, identities["user"]) is not None
    assert db.get(m.ExecutionCredential, identities["credential"]) is not None
    assert db.get(m.ExecutionStorageRoot, identities["root"]) is not None
    assert source.read_bytes() == b"do not delete source files"
    before = counts(db)
    assert purge_legacy_workflows(db) == {"removed": [], "deleted_counts": {}}
    db.commit()
    assert counts(db) == before


def test_uses_callers_transaction_and_never_accepts_custom_slug(environment):
    db, row, _ = environment
    identities = seed(db, row)
    before = counts(db)
    purge_legacy_workflows(db, slugs={"custom-kept", "electron-cross-section-gbt36422"})
    assert db.get(m.ExecutionWorkflow, identities["custom"]) is not None
    db.rollback()
    assert counts(db) == before
