"""Real PostgreSQL row-lock gates for replacement, publishing and Run creation."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

url = make_url(os.environ["TEST_DATABASE_URL"])
if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
    pytest.skip(
        "requires disposable PostgreSQL TEST_DATABASE_URL ending in _test",
        allow_module_level=True,
    )

from app.config import settings
from app.database import SessionLocal
from app.execution.catalog import ensure_default_catalog, publish_workflow
from app.execution.engine import create_run
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionCategory,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
)
from app.execution.release_v2 import (
    _release_digest,
    apply_release,
    preflight_release,
    publish_release,
    put_deployment_binding,
)
from app.execution.v2.examples import build_readonly_file_query_smoke_release
from app.execution.worker import ExecutionWorker
from app.execution.worker_state import record_worker_heartbeat
from app.execution.workflow_replacement import (
    replacement_view,
    source_snapshot,
    switch_replacement,
)
from tests.test_execution_workflow_replacement import replacement_payload


def _stage(db, actor, document, root):
    report = preflight_release(db, document=document, actor=actor)
    assert report["content_valid"], report
    release = apply_release(
        db, document=None, preflight_token=report["preflight_token"], actor=actor
    )
    put_deployment_binding(
        db,
        release_id=release.id,
        environment=settings.EXECUTION_ENVIRONMENT_ID,
        expected_revision=0,
        bindings={
            "root_slots": {
                "source": {"root_id": root.root_id, "revision": root.binding_revision}
            },
            "role_slots": {},
            "credential_slots": {},
            "rule_slots": {},
        },
        actor=actor,
    )
    return release


def _publish(db, actor, release, source=None):
    report = preflight_release(
        db,
        document=release.portable_document,
        actor=actor,
        release=release,
        scope="publish",
        replacement_source=source,
    )
    assert report["publish_ready"], report
    return publish_release(
        db,
        release_id=release.id,
        preflight_token=report["preflight_token"],
        actor=actor,
        reason="PostgreSQL gate",
    )[1]


@pytest.fixture(params=["source_first", "target_first"])
def pair(request, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_CONTRACT_MODE", "enforced")
    monkeypatch.setattr(settings, "EXECUTION_V2_ROLLOUT_PROFILE", "p2_publish")
    token = uuid4().hex
    with SessionLocal() as db:
        category = ExecutionCategory(key=f"pg-{token}", name="PG replacement")
        user = ExecutionUser(
            username=f"pg-{token}",
            display_name="PG replacement",
            password_hash="unused",
            role="admin",
        )
        root = ExecutionStorageRoot(
            root_id=f"pg-{token}",
            name="PG root",
            local_path=str(tmp_path),
            access_mode="read",
            is_available=True,
            is_active=True,
        )
        db.add_all([category, user, root])
        db.flush()
        definition = {
            "schema_version": "1.0",
            "input_schema": {"type": "object", "properties": {}},
            "global_schema": {"type": "object", "properties": {}},
            "nodes": [
                {"id": "start", "type": "core.start", "name": "开始", "config": {}},
                {"id": "end", "type": "core.end", "name": "结束", "config": {}},
            ],
            "edges": [{"source": "start", "target": "end"}],
            "root_slots": [],
            "credential_slots": [],
        }
        source = ExecutionWorkflow(
            id=("0000" if request.param == "source_first" else "ffff") + token,
            slug=f"pg-old-{token}",
            category_id=category.id,
            name="PG old",
            draft_definition=definition,
            capabilities={"read": True},
            is_enabled=True,
        )
        db.add(source)
        db.flush()
        publish_workflow(
            db,
            workflow_id=source.id,
            expected_revision=source.draft_revision,
            actor=user,
            release_note="PG source",
        )
        worker = ExecutionWorker(worker_id=f"pg-{token}")
        record_worker_heartbeat(
            db,
            worker_id=worker.worker_id,
            capability_document=worker.capability_document,
        )
        evidence = source_snapshot(db, source)
        document = build_readonly_file_query_smoke_release()
        document["release"].update(slug=f"pg-new-{token}", category_key=category.key)
        document["migration"] = {
            "source_format": "textile-execution-workflow",
            "source_format_version": "1.0",
            "source_digest": evidence["definition_checksum"],
        }
        document["integrity"]["digest"] = _release_digest(document)
        release = _stage(db, user, document, root)
        version = _publish(db, user, release, evidence)
        db.commit()
        value = SimpleNamespace(
            source_id=source.id,
            target_id=version.workflow_id,
            user_id=user.id,
            root_id=root.id,
            document=document,
            evidence=evidence,
        )
        value.state = replacement_view(db, value.target_id)
    # All identifiers are unique; the session-level disposable-schema fixture
    # removes these records. No shared tables are truncated during contenders.
    yield value
    with SessionLocal() as db:
        db.query(ExecutionRun).filter(
            ExecutionRun.workflow_id.in_([value.source_id, value.target_id])
        ).update({"status": "cancelled"}, synchronize_session=False)
        db.commit()


def _switch(db, pair, action="activate", state=None):
    return switch_replacement(
        db,
        workflow_id=pair.target_id,
        action=action,
        actor=db.get(ExecutionUser, pair.user_id),
        **replacement_payload(state or pair.state),
    )


def _run(db, pair, workflow_id, key):
    return create_run(
        db,
        workflow=db.get(ExecutionWorkflow, workflow_id),
        actor=db.get(ExecutionUser, pair.user_id),
        inspection_number="PG-001",
        input_data={},
        global_data={},
        idempotency_key=key,
    )


def _wait_on_lock(application_name):
    deadline = time.monotonic() + 8
    with SessionLocal() as observer:
        while time.monotonic() < deadline:
            waiting = observer.execute(
                text(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE application_name = :name"
                ),
                {"name": application_name},
            ).scalar()
            if waiting == "Lock":
                return
            time.sleep(0.02)
    raise AssertionError("contender did not wait on the Workflow row lock")


def _contend(application_name, action):
    with SessionLocal() as db:
        db.execute(
            text("SELECT set_config('application_name', :name, true)"),
            {"name": application_name},
        )
        try:
            result = action(db)
            db.commit()
            return result
        except ExecutionApiError as exc:
            db.rollback()
            return exc.code


def test_activation_serializes_stale_run_creation_and_has_no_half_switch(pair):
    ready, proceed = threading.Event(), threading.Event()
    application_name = "replacement-run-" + uuid4().hex

    def stale_create(db):
        source = db.get(ExecutionWorkflow, pair.source_id)
        assert source.is_enabled
        ready.set()
        assert proceed.wait(8)
        return _run(db, pair, source.id, str(uuid4()))

    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_contend, application_name, stale_create)
        assert ready.wait(8)
        _switch(holder, pair)
        proceed.set()
        _wait_on_lock(application_name)
        with SessionLocal() as observer:
            state = replacement_view(observer, pair.target_id)
            assert state["source"]["is_enabled"] and not state["target"]["is_enabled"]
        holder.commit()
        assert future.result(8) == "workflow_archived"
    with SessionLocal() as db:
        assert db.query(ExecutionRun).filter_by(workflow_id=pair.source_id).count() == 0


def test_run_created_before_activation_keeps_original_version(pair):
    application_name = "replacement-activate-" + uuid4().hex
    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        run, _ = _run(holder, pair, pair.source_id, str(uuid4()))
        original = run.workflow_version_id, run.definition_checksum
        run_id = run.id
        future = pool.submit(_contend, application_name, lambda db: _switch(db, pair))
        _wait_on_lock(application_name)
        holder.commit()
        assert future.result(8)["status"] == "active"
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert (run.workflow_version_id, run.definition_checksum) == original


def test_repeated_activation_and_revert_have_one_winner(pair):
    for action in ("activate", "revert"):
        with SessionLocal() as db:
            state = replacement_view(db, pair.target_id)
        barrier = threading.Barrier(2)

        def compete(db):
            barrier.wait(8)
            return _switch(db, pair, action, state)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_contend, "replacement-race-" + uuid4().hex, compete)
                for _ in range(2)
            ]
            results = [future.result(10) for future in futures]
        assert sum(isinstance(item, dict) for item in results) == 1
        assert "replacement_revision_changed" in results
    with SessionLocal() as db:
        assert (
            db.query(ExecutionAuditLog)
            .filter(
                ExecutionAuditLog.resource_id == pair.target_id,
                ExecutionAuditLog.action.like("workflow_replacement.%"),
            )
            .count()
            == 2
        )


@pytest.mark.parametrize("side", ["source", "target"])
def test_version_publish_winning_lock_invalidates_switch_expectations(pair, side):
    application_name = "replacement-version-" + uuid4().hex
    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        actor = holder.get(ExecutionUser, pair.user_id)
        if side == "source":
            source = holder.get(ExecutionWorkflow, pair.source_id)
            changed = deepcopy(source.draft_definition)
            changed["nodes"][1]["name"] = "并发修改的结束节点"
            source.draft_definition = changed
            source.draft_revision += 1
            holder.flush()
            publish_workflow(
                holder,
                workflow_id=source.id,
                expected_revision=source.draft_revision,
                actor=actor,
                release_note="concurrent source version",
            )
        else:
            document = deepcopy(pair.document)
            document["release"]["release_version"] = 2
            document["integrity"]["digest"] = _release_digest(document)
            staged = _stage(
                holder, actor, document, holder.get(ExecutionStorageRoot, pair.root_id)
            )
            _publish(holder, actor, staged)
        future = pool.submit(_contend, application_name, lambda db: _switch(db, pair))
        _wait_on_lock(application_name)
        holder.commit()
        assert future.result(8) == "replacement_revision_changed"
    with SessionLocal() as db:
        assert db.get(ExecutionWorkflow, pair.source_id).archived_at is None


def test_revert_blocks_new_target_run_without_changing_existing_run(pair):
    with SessionLocal() as db:
        state = _switch(db, pair)
        db.commit()
        run, _ = _run(db, pair, pair.target_id, str(uuid4()))
        original = run.workflow_version_id, deepcopy(run.dependency_lock)
        run_id = run.id
        db.commit()
    application_name = "replacement-revert-" + uuid4().hex
    with SessionLocal() as holder, ThreadPoolExecutor(max_workers=1) as pool:
        _switch(holder, pair, "revert", state)
        future = pool.submit(
            _contend,
            application_name,
            lambda db: _run(db, pair, pair.target_id, str(uuid4())),
        )
        _wait_on_lock(application_name)
        holder.commit()
        assert future.result(8) == "workflow_disabled"
    with SessionLocal() as db:
        run = db.get(ExecutionRun, run_id)
        assert (run.workflow_version_id, run.dependency_lock) == original


def test_duplicate_run_creation_is_serialized_and_idempotent(pair):
    with SessionLocal() as db:
        _switch(db, pair)
        db.commit()
    barrier = threading.Barrier(2)
    key = str(uuid4())

    def create(db):
        barrier.wait(8)
        run, duplicate = _run(db, pair, pair.target_id, key)
        return run.id, duplicate

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_contend, "replacement-duplicate-" + uuid4().hex, create)
            for _ in range(2)
        ]
        results = [future.result(10) for future in futures]
    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [False, True]
