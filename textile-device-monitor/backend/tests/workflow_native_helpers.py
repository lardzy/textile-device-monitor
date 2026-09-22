"""HTTP/Worker acceptance for P2 migration and local workflow replacement."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.execution import router as v1_router
from app.api.execution_v2 import router as v2_router
from app.config import settings
from app.database import Base, get_db
from app.execution.catalog import (
    bind_user_role,
    ensure_default_catalog,
    ensure_default_rbac,
)
from app.execution.engine import claim_next_node, execute_claimed_node
from app.execution.models import (
    ExecutionAuditLog,
    ExecutionFileIndexEntry,
    ExecutionHumanTask,
    ExecutionFileMutation,
    ExecutionPublishReceipt,
    ExecutionNodeRun,
    ExecutionRun,
    ExecutionStorageRoot,
    ExecutionUser,
    ExecutionWorkflow,
    ExecutionWorkflowRelease,
    ExecutionWorkflowVersion,
)
from app.execution.release_v2 import _release_digest
from app.execution.security import SESSION_COOKIE, create_session, hash_password
from app.execution.validation import definition_checksum
from app.execution.worker import ExecutionWorker
from app.execution.worker_state import record_worker_heartbeat


CHOICES = [
    "electron-source-selection",
    "hemp-cotton-source-selection",
    "special-wool-source-selection",
]
CANARY = "system-controlled-xlsx-write-test"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_CONTRACT_MODE", "enforced")
    monkeypatch.setattr(settings, "EXECUTION_V2_ROLLOUT_PROFILE", "p2_publish")
    monkeypatch.setattr(settings, "EXECUTION_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "EXECUTION_PUBLISH_ROOT", str(tmp_path / "publish"))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'replacement_test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    db = Session()
    ensure_default_rbac(db)
    ensure_default_catalog(db)
    admin = ExecutionUser(
        username="replacement-admin",
        display_name="接替验收",
        role="admin",
        password_hash=hash_password("test-password"),
    )
    operator = ExecutionUser(
        username="replacement-user",
        display_name="操作员",
        role="user",
        password_hash=hash_password("test-password"),
    )
    db.add_all([admin, operator])
    db.flush()
    for user in (admin, operator):
        bind_user_role(db, user, user.role, created_by_id=admin.id)
    roots = {}
    for root_id in (
        "electron_microscopy_records",
        "hemp_cotton_records",
        "special_wool_records",
        "paper_fiber_records",
        "execution_templates",
        "execution_staging",
        "execution_publish",
    ):
        path = tmp_path / root_id
        path.mkdir()
        root = ExecutionStorageRoot(
            root_id=root_id,
            name=root_id,
            local_path=str(path),
            access_mode=(
                "publish"
                if root_id.endswith("publish")
                else "write" if root_id.endswith("staging") else "read"
            ),
            is_active=True,
            is_available=True,
            scan_generation=1,
        )
        db.add(root)
        roots[root_id] = root
    worker = ExecutionWorker(worker_id="replacement-worker")
    record_worker_heartbeat(
        db, worker_id=worker.worker_id, capability_document=worker.capability_document
    )
    _session, token, csrf = create_session(db, admin)
    _user_session, user_token, user_csrf = create_session(db, operator)
    db.commit()
    app = FastAPI()
    app.include_router(v1_router, prefix="/api")
    app.include_router(v2_router, prefix="/api")

    def session_dependency():
        with Session() as request_db:
            yield request_db

    app.dependency_overrides[get_db] = session_dependency
    with TestClient(app) as client, TestClient(app) as user_client:
        client.cookies.set(SESSION_COOKIE, token)
        client.headers["X-CSRF-Token"] = csrf
        user_client.cookies.set(SESSION_COOKIE, user_token)
        user_client.headers["X-CSRF-Token"] = user_csrf
        yield SimpleNamespace(
            db=db,
            Session=Session,
            client=client,
            user_client=user_client,
            admin=admin,
            roots=roots,
            path=tmp_path,
            worker=worker,
        )
    db.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def request(env, method, path, payload=None, *, status=200):
    response = env.client.request(method, f"/api/execution/{path}", json=payload)
    assert response.status_code == status, response.text
    return response.json()


def stage(env, document):
    content = request(
        env, "POST", "v2/workflow-releases/preflight", {"document": document}
    )
    assert content["content_valid"], content
    release = request(
        env,
        "POST",
        "v2/workflow-releases/apply",
        {"preflight_token": content["preflight_token"]},
        status=201,
    )
    request(
        env,
        "PUT",
        f"v2/workflow-releases/{release['id']}/deployment-binding",
        {
            "environment": "default",
            "expected_revision": 0,
            "root_bindings": [
                {
                    "slot": slot["slot_id"],
                    "storage_root_id": env.roots[slot["slot_id"]].id,
                    "role": slot["access"],
                }
                for slot in document["resources"]["root_slots"]
            ],
        },
    )
    return release


def publish(env, release, source=None):
    report = request(
        env,
        "POST",
        f"v2/workflow-releases/{release['id']}/preflight",
        {"replacement_source": source} if source else {},
    )
    assert report["publish_ready"], report
    return request(
        env,
        "POST",
        f"v2/workflow-releases/{release['id']}/publish",
        {"preflight_token": report["preflight_token"]},
    )


def run(env, workflow_id, *, inputs=None, key=None):
    return request(
        env,
        "POST",
        "v1/runs",
        {
            "workflow_id": workflow_id,
            "inspection_number": (inputs or {}).get("inspection_number", "P2-001"),
            "input_data": inputs or {},
            "global_data": {},
            "idempotency_key": key or str(uuid4()),
        },
        status=201,
    )["run"]


def drain(env):
    """Run actual Worker claim/lease/execute until the DAG suspends or ends."""
    for _ in range(30):
        with env.Session() as db:
            claimed = claim_next_node(db, worker_id=env.worker.worker_id)
            if claimed is None:
                return
            node_id, lease_token = claimed.id, claimed.lease_token
            db.commit()
            execute_claimed_node(db, node_id, lease_token)
            db.commit()
    raise AssertionError("DAG did not quiesce")
