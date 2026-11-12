"""Deployment rehearsals use actual Release services and disposable storage."""
import json
import os
from copy import deepcopy
from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from app.execution import deploy_builtin_workflows as deployment
from app.execution.catalog import ensure_default_catalog, ensure_default_rbac
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionAuditLog, ExecutionCategory, ExecutionCredential,
    ExecutionDeploymentBinding, ExecutionExternalOperation, ExecutionHumanTask,
    ExecutionNodeRun, ExecutionRun,
    ExecutionStorageRoot, ExecutionUser, ExecutionWorkflow,
    ExecutionWorkflowRelease, ExecutionWorkflowVersion, utcnow,
)
from app.execution.worker import ExecutionWorker
from app.execution.worker_state import record_worker_heartbeat


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_CONTRACT_MODE", "enforced")
    monkeypatch.setattr(settings, "EXECUTION_V2_ROLLOUT_PROFILE", "p2_publish")
    postgres = os.environ.get("EXECUTION_DEPLOYMENT_TEST_POSTGRES") == "1"
    engine = create_engine(os.environ["TEST_DATABASE_URL"] if postgres else f"sqlite:///{tmp_path / 'builtin_test.db'}")
    if postgres:
        # conftest additionally guards every DDL target by its _test suffix.
        assert engine.dialect.name == "postgresql" and engine.url.database.endswith("_test")
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        ensure_default_catalog(db)
        ensure_default_rbac(db)
        actor = ExecutionUser(username="operator", display_name="Operator", role="admin", password_hash="test")
        db.add(actor)
        db.flush()
        db.add(ExecutionCredential(user_id=actor.id, system_key="legacy_inspection", account_name="test", encrypted_secret="never-output-this-secret", is_active=True))
        slots = {slot["slot_id"]: slot for slug in deployment.BUILTIN_SLUGS
                 for slot in json.loads((deployment.RESOURCE_DIRECTORY / f"{slug}.json").read_text())["resources"]["root_slots"]}
        for key, slot in slots.items():
            directory = tmp_path / key
            directory.mkdir()
            db.add(ExecutionStorageRoot(root_id=key, name=key, local_path=str(directory), access_mode=slot["access"], is_active=True, is_available=True))
        for source in (Path(deployment.__file__).parent / "templates").glob("*.xls*"):
            shutil.copyfile(source, tmp_path / "execution_templates" / source.name)
        (tmp_path / "electron_microscopy_records" / "keep.bmp").write_bytes(b"original-image")
        category = db.query(ExecutionCategory).filter(ExecutionCategory.key == "electron_microscopy").one()
        old = ExecutionWorkflow(slug="electron-cross-section-gbt36422", name="旧横截面", category_id=category.id, draft_definition={"nodes": [{"id": "old", "type": "retired"}]}, management_mode="draft_v1", is_enabled=True)
        custom = ExecutionWorkflow(slug="my-custom-workflow", name="Custom", category_id=category.id, draft_definition={}, management_mode="draft_v1", is_enabled=True)
        db.add_all([old, custom])
        db.flush()
        old_version = ExecutionWorkflowVersion(workflow_id=old.id, version_number=2, schema_version="1.0", definition=old.draft_definition, checksum="a" * 64, contract_checksum="b" * 64)
        db.add(old_version)
        db.flush()
        old.published_version_number = 2
        history = ExecutionRun(workflow_id=old.id, workflow_version_id=old_version.id, created_by_id=actor.id, idempotency_key="existing", inspection_number="kept-number", status="completed", definition_snapshot=old.draft_definition, definition_checksum="a" * 64, contract_checksum="b" * 64)
        db.add(history)
        worker = ExecutionWorker(worker_id="deployment-test")
        record_worker_heartbeat(db, worker_id=worker.worker_id, capability_document=worker.capability_document)
        db.commit()
        yield db, tmp_path, old.id, custom.id, history.id
    if postgres:
        Base.metadata.drop_all(engine)
        # The session-scoped conftest owns the schema for subsequent test files.
        # Restore its empty baseline rather than leaving them without tables.
        Base.metadata.create_all(engine)
    engine.dispose()


def counts(db):
    return {table.name: db.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables}


def test_preview_rolls_back_real_publication_and_keeps_files(environment):
    db, path, old_id, custom_id, history_id = environment
    before = counts(db)
    files = {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file() and p.suffix != ".db"}
    report = deployment.deploy(db)
    assert report["applied"] is False
    assert len(report["workflows"]) == 2
    assert report["retired"][0]["changed"] is True
    assert counts(db) == before
    assert db.get(ExecutionWorkflow, old_id).is_enabled
    assert db.get(ExecutionWorkflow, old_id).archived_at is None
    assert db.get(ExecutionRun, history_id).status == "completed"
    assert files == {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file() and p.suffix != ".db"}
    assert "never-output-this-secret" not in json.dumps(report)


def test_apply_then_repeat_is_idempotent_and_keeps_custom_and_history(environment):
    db, path, old_id, custom_id, history_id = environment
    first = deployment.deploy(db, apply=True)
    old = db.get(ExecutionWorkflow, old_id)
    assert old.archived_at is not None and not old.is_enabled
    revision = old.draft_revision
    assert db.get(ExecutionWorkflow, custom_id).is_enabled
    assert db.get(ExecutionWorkflow, custom_id).archived_at is None
    assert db.get(ExecutionRun, history_id).definition_snapshot["nodes"][0]["type"] == "retired"
    assert (path / "electron_microscopy_records" / "keep.bmp").read_bytes() == b"original-image"
    version_count = db.query(ExecutionWorkflowVersion).count()
    release_count = db.query(ExecutionWorkflowRelease).count()
    binding_count = db.query(ExecutionDeploymentBinding).count()
    audit_count = db.query(ExecutionAuditLog).count()
    all_counts = counts(db)
    second = deployment.deploy(db, apply=True)
    assert all(item["unchanged"] for item in second["workflows"])
    assert not second["retired"][0]["changed"]
    assert db.get(ExecutionWorkflow, old_id).draft_revision == revision
    assert db.query(ExecutionWorkflowVersion).count() == version_count
    assert db.query(ExecutionWorkflowRelease).count() == release_count
    assert db.query(ExecutionDeploymentBinding).count() == binding_count
    assert db.query(ExecutionAuditLog).count() == audit_count
    assert counts(db) == all_counts
    assert {item["workflow_id"] for item in first["workflows"]} == {item["workflow_id"] for item in second["workflows"]}


def test_second_publication_failure_rolls_back_first(environment, monkeypatch):
    db, _path, old_id, _custom_id, _history_id = environment
    before = counts(db)
    original = deployment.publish_release
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(kwargs["release_id"])
        if len(calls) == 2:
            raise RuntimeError("simulated second publication failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(deployment, "publish_release", fail_second)
    with pytest.raises(RuntimeError, match="second publication"):
        deployment.deploy(db, apply=True)
    assert len(calls) == 2
    assert counts(db) == before
    assert db.get(ExecutionWorkflow, old_id).archived_at is None


@pytest.mark.parametrize("pending_kind", ["run", "external"])
def test_inflight_work_blocks_retirement_without_cancelling(environment, pending_kind):
    db, _path, old_id, _custom_id, history_id = environment
    run = db.get(ExecutionRun, history_id)
    if pending_kind == "run":
        run.status = "waiting_human"
    else:
        db.add(ExecutionExternalOperation(operation_key="pending", run_id=run.id, connector_key="legacy_fibrecheck", credential_revision=1, account_scope_key="scope", remote_business_key="scope", status="reconciliation_required", payload_checksum="c" * 64, preflight_expires_at=utcnow()))
    db.commit()
    before = counts(db)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, apply=True)
    assert caught.value.code == "deployment_work_in_progress"
    assert counts(db) == before
    assert db.get(ExecutionWorkflow, old_id).is_enabled
    assert db.get(ExecutionRun, history_id).status == ("waiting_human" if pending_kind == "run" else "completed")


@pytest.mark.parametrize("status, blocks", [("open", True), ("claimed", True), ("rejected", False), ("completed_rejected_contract", False)])
def test_human_task_terminal_statuses_do_not_block(environment, status, blocks):
    db, _path, old_id, _custom_id, history_id = environment
    db.get(ExecutionRun, history_id).status = "failed"
    node = ExecutionNodeRun(run_id=history_id, node_id="human", node_type="human.input", node_name="Historical", status="failed")
    db.add(node)
    db.flush()
    task = ExecutionHumanTask(run_id=history_id, node_run_id=node.id, title="Historical", status=status)
    db.add(task)
    db.commit()
    task_id = task.id
    if blocks:
        with pytest.raises(ExecutionApiError) as caught:
            deployment.deploy(db, apply=True)
        assert caught.value.code == "deployment_work_in_progress"
        assert db.get(ExecutionWorkflow, old_id).is_enabled
    else:
        deployment.deploy(db, apply=True)
        assert not db.get(ExecutionWorkflow, old_id).is_enabled
    assert db.get(ExecutionHumanTask, task_id).status == status


def test_missing_root_fails_without_partial_release(environment):
    db, _path, old_id, _custom_id, _history_id = environment
    db.query(ExecutionStorageRoot).filter(ExecutionStorageRoot.root_id == "paper_fiber_records").one().is_available = False
    db.commit()
    before = counts(db)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, apply=True)
    assert caught.value.code == "deployment_root_unavailable"
    assert counts(db) == before
    assert db.get(ExecutionWorkflow, old_id).is_enabled


def test_existing_other_v2_version_is_not_overwritten(environment):
    db, _path, _old_id, _custom_id, _history_id = environment
    deployment.deploy(db, apply=True)
    target = db.query(ExecutionWorkflow).filter(ExecutionWorkflow.slug == "fiber-microscopy-v2").one()
    version = db.query(ExecutionWorkflowVersion).filter(ExecutionWorkflowVersion.workflow_id == target.id).one()
    version.release_digest = "f" * 64
    db.commit()
    before = counts(db)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, apply=True)
    assert caught.value.code == "deployment_existing_workflow"
    assert counts(db) == before


def test_missing_live_worker_blocks_publication(environment):
    from app.execution.models import ExecutionWorkerHeartbeat
    db, _path, old_id, _custom_id, _history_id = environment
    db.query(ExecutionWorkerHeartbeat).one().status = "stopped"
    db.commit()
    before = counts(db)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, apply=True)
    assert caught.value.code == "deployment_preflight_failed"
    assert any(issue["code"] == "node_capability_unavailable" for issue in caught.value.details["issues"])
    assert counts(db) == before
    assert db.get(ExecutionWorkflow, old_id).is_enabled


@pytest.mark.parametrize("disabled", [True, False])
def test_unavailable_existing_target_cannot_retire_old_entry(environment, disabled):
    db, _path, old_id, _custom_id, _history_id = environment
    deployment.deploy(db, apply=True)
    old = db.get(ExecutionWorkflow, old_id)
    old.archived_at = None
    old.is_enabled = True
    target = db.query(ExecutionWorkflow).filter(ExecutionWorkflow.slug == "fiber-microscopy-v2").one()
    if disabled:
        target.is_enabled = False
    else:
        target.availability_code = "root_not_configured"
    db.commit()
    before = counts(db)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, apply=True)
    assert caught.value.code == "deployment_target_unavailable"
    assert counts(db) == before
    assert db.get(ExecutionWorkflow, old_id).archived_at is None
    assert db.get(ExecutionWorkflow, old_id).is_enabled


def test_repeated_deployment_preserves_deliberately_disabled_target(environment):
    db, _path, _old_id, _custom_id, _history_id = environment
    deployment.deploy(db, apply=True)
    target = db.query(ExecutionWorkflow).filter(ExecutionWorkflow.slug == "fiber-microscopy-v2").one()
    target.is_enabled = False
    db.commit()
    before = counts(db)
    report = deployment.deploy(db, apply=True)
    assert counts(db) == before
    assert next(item for item in report["workflows"] if item["slug"] == target.slug)["is_enabled"] is False
    assert not db.get(ExecutionWorkflow, target.id).is_enabled


def alternate_bundle(directory, *, version_delta=0, change_name=False):
    directory.mkdir()
    for slug in deployment.BUILTIN_SLUGS:
        document = json.loads((deployment.RESOURCE_DIRECTORY / f"{slug}.json").read_text())
        document["release"]["release_version"] += version_delta
        if change_name:
            document["release"]["name"] += " - custom"
        document["integrity"]["digest"] = deployment._release_digest(document)
        (directory / f"{slug}.json").write_text(json.dumps(document, ensure_ascii=False))
    return directory


def test_explicit_sync_upgrades_both_preserving_active_v2_run(environment, monkeypatch):
    db, path, _old_id, _custom_id, history_id = environment
    bundled = deployment.RESOURCE_DIRECTORY
    old_bundle = alternate_bundle(path / "old-bundle", version_delta=-1)
    monkeypatch.setattr(deployment, "RESOURCE_DIRECTORY", old_bundle)
    old_report = deployment.deploy(db, apply=True)
    target = db.get(ExecutionWorkflow, old_report["workflows"][0]["workflow_id"])
    old_version = db.query(ExecutionWorkflowVersion).filter(ExecutionWorkflowVersion.workflow_id == target.id).one()
    actor_id = db.get(ExecutionRun, history_id).created_by_id
    active = ExecutionRun(workflow_id=target.id, workflow_version_id=old_version.id, created_by_id=actor_id,
        idempotency_key="old-native", inspection_number="in-flight-native", status="waiting_human",
        definition_snapshot=old_version.definition, definition_checksum=old_version.checksum,
        contract_checksum=old_version.contract_checksum, release_id=old_version.release_id,
        release_digest=old_version.release_digest)
    db.add(active)
    target.is_enabled = False
    db.commit()
    active_id, old_version_id = active.id, old_version.id
    frozen = json.dumps(active.definition_snapshot, sort_keys=True)
    monkeypatch.setattr(deployment, "RESOURCE_DIRECTORY", bundled)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, apply=True)
    assert caught.value.code == "deployment_existing_workflow"
    before = counts(db)
    preview = deployment.deploy(db, sync_existing=True)
    assert not preview["applied"]
    assert all(item["version"] == 2 and item["is_enabled"] for item in preview["workflows"])
    assert counts(db) == before
    assert not db.get(ExecutionWorkflow, target.id).is_enabled
    report = deployment.deploy(db, sync_existing=True, apply=True)
    assert all(item["version"] == 2 and item["is_enabled"] and item["worker_ready"] for item in report["workflows"])
    assert all(item["release_digest"] and item["dependency_lock_digest"] and item["deployment_binding_digest"] for item in report["workflows"])
    active = db.get(ExecutionRun, active_id)
    assert active.status == "waiting_human"
    assert active.workflow_version_id == old_version_id
    assert json.dumps(active.definition_snapshot, sort_keys=True) == frozen
    before_repeat = counts(db)
    assert all(item["unchanged"] for item in deployment.deploy(db, sync_existing=True, apply=True)["workflows"])
    assert counts(db) == before_repeat


@pytest.mark.parametrize("version_delta, change_name, code", [
    (1, False, "deployment_newer_release"),
    (0, True, "deployment_release_conflict"),
])
def test_explicit_sync_never_downgrades_or_rewrites_source_identity(environment, monkeypatch, version_delta, change_name, code):
    db, path, _old_id, _custom_id, _history_id = environment
    bundled = deployment.RESOURCE_DIRECTORY
    custom_bundle = alternate_bundle(path / "custom-bundle", version_delta=version_delta, change_name=change_name)
    monkeypatch.setattr(deployment, "RESOURCE_DIRECTORY", custom_bundle)
    deployment.deploy(db, apply=True)
    monkeypatch.setattr(deployment, "RESOURCE_DIRECTORY", bundled)
    before = counts(db)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, sync_existing=True, apply=True)
    assert caught.value.code == code
    assert counts(db) == before


def test_explicit_sync_reenables_current_target_without_new_version(environment):
    db, _path, _old_id, _custom_id, _history_id = environment
    deployment.deploy(db, apply=True)
    target = db.query(ExecutionWorkflow).filter(ExecutionWorkflow.slug == "paper-fiber-v2").one()
    target.is_enabled = False
    db.commit()
    version_count = db.query(ExecutionWorkflowVersion).count()
    report = deployment.deploy(db, sync_existing=True, apply=True)
    assert all(item["is_enabled"] for item in report["workflows"])
    assert db.query(ExecutionWorkflowVersion).count() == version_count
    assert db.query(ExecutionAuditLog).filter(ExecutionAuditLog.action == "workflow.builtin_enable").count() == 1


def test_concurrent_first_publication_is_not_overwritten(environment, monkeypatch):
    db, _path, _old_id, _custom_id, _history_id = environment
    original = deployment.publish_release
    def simulate_concurrent_winner(*args, **kwargs):
        release, version, receipt = original(*args, **kwargs)
        version.version_number = 2
        return release, version, receipt
    monkeypatch.setattr(deployment, "publish_release", simulate_concurrent_winner)
    before = counts(db)
    with pytest.raises(ExecutionApiError) as caught:
        deployment.deploy(db, apply=True)
    assert caught.value.code == "deployment_target_changed"
    assert counts(db) == before


@pytest.mark.skipif(os.environ.get("EXECUTION_DEPLOYMENT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL _test database")
def test_postgres_concurrent_deployment_publishes_once(environment):
    db, _path, old_id, _custom_id, _history_id = environment
    engine = db.get_bind()
    db.rollback()
    db.expunge_all()
    start = Barrier(2)
    def apply_once():
        with Session(engine, autoflush=False) as other:
            start.wait(timeout=5)
            return deployment.deploy(other, sync_existing=True, apply=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply_once) for _ in range(2)]
        reports = [future.result(timeout=30) for future in futures]
    assert sorted(all(item["unchanged"] for item in report["workflows"]) for report in reports) == [False, True]
    assert db.query(ExecutionWorkflowRelease).count() == 2
    assert db.query(ExecutionWorkflowVersion).count() == 3  # one preserved v1, two native versions
    assert db.query(ExecutionAuditLog).filter(ExecutionAuditLog.action == "workflow.legacy_retire").count() == 1
    assert not db.get(ExecutionWorkflow, old_id).is_enabled


def test_explicit_sync_updates_old_designer_draft_and_keeps_publishable_bindings(environment):
    from app.execution.v2.drafts import save_draft, view_draft
    from app.execution.v2.designer import compile_document
    db, _path, _old_id, _custom_id, _history_id = environment
    deployment.deploy(db, apply=True)
    target = db.query(ExecutionWorkflow).filter(ExecutionWorkflow.slug == "fiber-microscopy-v2").one()
    document = json.loads((deployment.RESOURCE_DIRECTORY / "fiber-microscopy-v2.json").read_text())
    old_draft = deepcopy(document)
    next(n for n in old_draft["definition"]["nodes"] if n["id"] == "profiles")["input_mapping"]["profiles"] = next(n for n in old_draft["definition"]["nodes"] if n["id"] == "profiles")["input_mapping"]["profiles"][:2]
    target.designer_draft = {"document": old_draft, "bindings": {"root_slots": {"execution_templates": {"root_id": "wrong-root"}}}}
    db.commit()
    before = deepcopy(target.designer_draft)
    revision = target.draft_revision
    before_counts = counts(db)
    preview = deployment.deploy(db, sync_existing=True)
    assert next(item for item in preview["workflows"] if item["slug"] == target.slug)["draft_changed"]
    assert db.get(ExecutionWorkflow, target.id).designer_draft == before
    assert target.draft_revision == revision
    assert counts(db) == before_counts
    report = deployment.deploy(db, sync_existing=True, apply=True)
    assert next(item for item in report["workflows"] if item["slug"] == target.slug)["draft_changed"]
    target = db.get(ExecutionWorkflow, target.id)
    assert target.draft_revision == revision + 1
    assert "integrity" not in target.designer_draft["document"]
    viewed = view_draft(db, target)
    assert viewed["document"]["release"]["release_version"] == document["release"]["release_version"] + 1
    profiles = next(n for n in viewed["document"]["definition"]["nodes"] if n["id"] == "profiles")["input_mapping"]["profiles"]
    assert len(profiles) == 3 and any(p["id"] == "cross-section-gbt36422" for p in profiles)
    assert all(set(binding) == {"root_id", "revision"} for binding in viewed["bindings"]["root_slots"].values())
    assert db.query(ExecutionAuditLog).filter(ExecutionAuditLog.action == "workflow.draft_saved", ExecutionAuditLog.resource_id == target.id).count() == 1
    repeat_counts = counts(db)
    repeat = deployment.deploy(db, sync_existing=True, apply=True)
    assert all(item["unchanged"] and not item["draft_changed"] for item in repeat["workflows"])
    assert counts(db) == repeat_counts
    # Exercise the actual save/compiler/bind/publish path used by the designer.
    actor = db.query(ExecutionUser).filter(ExecutionUser.username == "operator").one()
    save_draft(db, document=viewed["document"], bindings=viewed["bindings"], actor=actor,
        workflow_id=target.id, revision=target.draft_revision)
    compiled = compile_document(viewed["document"])
    assert compiled["content_valid"], compiled["issues"]
    content = deployment.preflight_release(db, document=compiled["document"], actor=actor)
    assert content["content_valid"], content["issues"]
    release = deployment.apply_release(db, document=None, preflight_token=content["preflight_token"], actor=actor)
    deployment.put_deployment_binding(db, release_id=release.id, environment=settings.EXECUTION_ENVIRONMENT_ID,
        expected_revision=0, bindings=viewed["bindings"], actor=actor)
    publish = deployment.preflight_release(db, document=compiled["document"], release=release, scope="publish", actor=actor)
    assert publish["publish_ready"], publish["issues"]
    _, version, _ = deployment.publish_release(db, release_id=release.id, preflight_token=publish["preflight_token"], actor=actor, reason="designer regression")
    assert version.version_number == 2
    db.rollback()
