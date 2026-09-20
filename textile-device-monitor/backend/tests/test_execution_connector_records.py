"""Record lookups share the task cache without treating missing detail as empty."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.config import settings
from app.execution.connector_records import record_fingerprint
from app.execution.engine import claim_next_node, execute_claimed_node
from app.execution.models import ExecutionExternalOperation, ExecutionHumanTask, ExecutionTaskSnapshotCache, utcnow
from app.execution.v2.examples import build_connector_query_smoke_release
from app.execution.v2.registry import get_installed_registry
from app.execution.worker_state import record_worker_heartbeat
from tests.test_execution_domain_services import NUMBER, task_snapshot
from tests.test_execution_workflow_replacement import environment, request, stage, publish, drain


@pytest.fixture
def records_env(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(settings, "EXECUTION_BRIDGE_TOKEN", "record-reader")
    task_snapshot(env)
    env.snapshot = deepcopy(env.db.get(ExecutionTaskSnapshotCache, NUMBER).snapshot)
    env.project = env.snapshot["projects"][0]
    env.project["project_key"] = "task-project:" + "1" * 24
    env.db.get(ExecutionTaskSnapshotCache, NUMBER).snapshot = deepcopy(env.snapshot)
    env.db.commit()
    env.headers = {"X-Execution-Bridge-Key": "record-reader"}
    return env


def record(env):
    value = {"record_ref": "check-record:sha256:" + "a" * 16, "record_kind": "generic",
             "register": {"ID": "sha256:" + "a" * 16, "SampleNo": NUMBER,
                          "CheckItemID": env.project["check_item_id"]},
             "generic_record": {"Remark": "原记录"}, "details": [{"RealValue": "木浆"}],
             "key_results": [], "list_data": [], "other_data": [], "association_issues": []}
    value["content_fingerprint"] = record_fingerprint(value)
    return value


def test_legacy_insert_without_new_columns_keeps_task_only_scope(environment):
    env = environment
    env.db.execute(text(
        "INSERT INTO execution_task_snapshot_cache "
        "(inspection_number, status, snapshot, revision, refresh_requested_at, created_at, updated_at) "
        "VALUES (:number, 'queued', '{}', 1, :now, :now, :now)"
    ), {"number": NUMBER, "now": utcnow()})
    env.db.commit()
    row = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    assert row.include_check_records is False and row.check_records is None


def query(env, *, single=False, status=200, **values):
    return request(env, "POST", "v1/connector-queries", {
        "query_ref": "legacy_fibrecheck.check_record." + ("get" if single else "list") + "@1",
        "input": {"inspection_number": NUMBER, "project_key": env.project["project_key"], **values},
    }, status=status)


def claim(env, *, capable=True):
    response = env.client.post("/api/execution/v1/task-snapshot-bridge/claim", headers=env.headers,
                               json={"bridge_id": "reader", "supports_check_records": capable})
    assert response.status_code == 200, response.text
    return response.json()


def complete(env, claimed, records, *, status=200):
    snapshot = deepcopy(env.snapshot)
    if records is not None:
        snapshot["check_records"] = {"schema_version": 1, "records": records}
    response = env.client.post(f"/api/execution/v1/task-snapshot-bridge/{NUMBER}/complete", headers=env.headers,
                               json={"bridge_id": "reader", "claim_token": claimed["claim_token"], "snapshot": snapshot})
    assert response.status_code == status, response.text
    return response.json()


def ready(env):
    pending = query(env, status=202)
    assert pending["result"]["records"] is None
    assert claim(env, capable=False)["claimed"] is False
    claimed = claim(env)
    assert claimed["include_check_records"] is True
    complete(env, claimed, [record(env)])


def test_read_scope_bridge_roundtrip_exact_record_and_regular_task_compatibility(records_env):
    env = records_env
    ready(env)
    result = query(env)["result"]
    assert result["cache_state"] == "ready" and result["lookup_state"] == "found"
    assert result["records"] == [record(env)]
    assert query(env, single=True, record_ref=record(env)["record_ref"])["result"]["record"] == record(env)
    assert query(env, single=True, record_ref="check-record:sha256:" + "b" * 16)["result"]["lookup_state"] == "not_found"
    assert query(env, project_key="task-project:" + "0" * 24)["result"]["records"] == []
    regular = request(env, "POST", "v1/connector-queries", {
        "query_ref": "legacy_fibrecheck.task_snapshot.get@1", "input": {"inspection_number": NUMBER},
    })
    assert "check_records" not in regular["result"]["snapshot"]
    assert env.db.query(ExecutionExternalOperation).count() == env.db.query(ExecutionHumanTask).count() == 0


def test_scope_upgrade_during_old_bridge_claim_requeues_without_losing_task_result(records_env):
    env = records_env
    request(env, "POST", "v1/connector-queries", {"query_ref": "legacy_fibrecheck.task_snapshot.get@1",
            "input": {"inspection_number": NUMBER, "refresh": True}}, status=202)
    old = claim(env, capable=False)
    assert not old["include_check_records"]
    query(env, status=202)
    assert complete(env, old, None)["status"] == "queued"
    assert claim(env, capable=False)["claimed"] is False
    new = claim(env)
    complete(env, new, [])
    empty = query(env)["result"]
    assert empty["records"] == [] and empty["lookup_state"] == "not_found"


@pytest.mark.parametrize("bad", ["sample", "ref", "fingerprint", "duplicate"])
def test_invalid_bridge_identity_or_contents_never_become_readable(records_env, bad):
    env = records_env
    query(env, status=202)
    claimed = claim(env)
    value = record(env)
    if bad == "sample":
        value["register"]["SampleNo"] = "260000001"
        value["content_fingerprint"] = record_fingerprint(value)
    elif bad == "ref":
        value["record_ref"] = "check-record:sha256:" + "b" * 16
        value["content_fingerprint"] = record_fingerprint(value)
    elif bad == "fingerprint":
        value["details"][0]["RealValue"] = "changed"
    complete(env, claimed, [value, value] if bad == "duplicate" else [value], status=422)
    assert query(env, status=202)["result"]["records"] is None


def test_stale_values_are_marked_and_refreshes_coalesce(records_env):
    env = records_env
    ready(env)
    env.db.expire_all()
    row = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    row.expires_at = utcnow() - timedelta(seconds=1)
    env.db.commit()
    stale = query(env, status=202)["result"]
    assert stale["cache_state"] == "stale" and stale["records"] == [record(env)]
    claimed = claim(env)
    first = query(env, refresh=True, status=202)["result"]
    second = query(env, refresh=True, status=202)["result"]
    assert first["revision"] == second["revision"]
    assert claim(env)["claimed"] is False
    changed = record(env)
    changed["details"][0]["RealValue"] = "竹浆"
    changed["content_fingerprint"] = record_fingerprint(changed)
    complete(env, claimed, [changed])
    assert query(env)["result"]["records"][0]["content_fingerprint"] != record(env)["content_fingerprint"]


def test_native_record_query_needs_exact_query_capability(records_env):
    env = records_env
    ready(env)
    release = publish(env, stage(env, build_connector_query_smoke_release("check_record.list")))
    run = request(env, "POST", "v1/runs", {"workflow_id": release["workflow_id"],
                  "inspection_number": NUMBER, "input_data": {"project_key": env.project["project_key"]},
                  "idempotency_key": str(uuid4())}, status=201)["run"]
    node = claim_next_node(env.db, worker_id=env.worker.worker_id)
    execute_claimed_node(env.db, node_run_id=node.id, lease_token=node.lease_token)
    env.db.commit()
    registry = get_installed_registry()
    task_only = registry.query_node_binding(registry.executable_binding_for("connector.query", 1),
                                           registry.connectors.resolve_query("legacy_fibrecheck", "*", "task_snapshot.get", 1))
    capability = deepcopy(env.worker.capability_document)
    capability["nodes"] = [node for node in capability["nodes"] if node["type"] != "connector.query"] + [task_only]
    capability["capability_digest"] = "c" * 64
    record_worker_heartbeat(env.db, worker_id="tasks-only", capability_document=capability)
    env.db.commit()
    assert claim_next_node(env.db, worker_id="tasks-only") is None
    drain(env)
    result = request(env, "GET", f"v1/runs/{run['id']}")
    assert result["status"] == "completed", result
    assert result["output_data"] == query(env)["result"]
