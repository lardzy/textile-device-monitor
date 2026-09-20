"""P4 direct reads and exact QuerySpec execution share one cache/Bridge queue."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.config import settings
from app.execution.engine import claim_next_node, execute_claimed_node
from app.execution.models import (
    ExecutionExternalOperation, ExecutionHumanTask, ExecutionRun,
    ExecutionTaskSnapshotCache, utcnow,
)
from app.execution.v2.examples import build_connector_query_smoke_release, _seal
from app.execution.v2.registry import get_installed_registry, resolve_connector_reference
from app.execution.worker_state import record_worker_heartbeat
from tests.test_execution_domain_services import NUMBER, task_snapshot
from tests.test_execution_workflow_replacement import environment, drain, publish, request, stage


QUERY = "legacy_fibrecheck.task_snapshot.get@1"


def query(env, data=None, *, status=200, **pins):
    return request(env, "POST", "v1/connector-queries", {
        "query_ref": QUERY, "input": data or {"inspection_number": NUMBER}, **pins,
    }, status=status)


def test_api_reads_existing_cache_without_a_run_or_refresh(environment):
    env = environment
    snapshot = task_snapshot(env)
    capabilities = request(env, "GET", "v1/connectors/legacy_fibrecheck/capabilities")
    spec = capabilities["queries"][0]
    assert spec["query_ref"] == QUERY and spec["direct_api_available"]
    assert len(capabilities["operations"]) == 9
    assert all(operation["direct_api_available"] for operation in capabilities["operations"])
    result = query(env, connector_version=spec["connector_version"], contract_digest=spec["contract_digest"])
    assert result["result"]["snapshot"] == snapshot
    assert result["result"]["cache_state"] == "ready"
    assert result["refresh_request"] is None
    assert result["remote_write_performed"] is False
    assert env.db.query(ExecutionRun).count() == 0
    assert env.db.query(ExecutionExternalOperation).count() == 0
    assert env.db.query(ExecutionTaskSnapshotCache).one().status == "ready"
    assert env.user_client.post("/api/execution/v1/connector-queries", json={
        "query_ref": QUERY, "input": {"inspection_number": NUMBER},
    }).status_code == 200


def test_api_queue_bridge_readback_and_repeat_requests(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(settings, "EXECUTION_BRIDGE_TOKEN", "p4-read-test")
    first = query(env, status=202)
    repeated = query(env, {"inspection_number": NUMBER, "refresh": True}, status=202)
    assert first["result"]["cache_state"] == "pending"
    assert repeated["result"]["revision"] == first["result"]["revision"]
    assert first["refresh_request"]["inspection_number"] == NUMBER
    assert env.db.query(ExecutionTaskSnapshotCache).count() == 1
    headers = {"X-Execution-Bridge-Key": "p4-read-test"}
    claimed = env.client.post("/api/execution/v1/task-snapshot-bridge/claim",
                              json={"bridge_id": "p4-reader"}, headers=headers).json()
    assert claimed["claimed"] is True
    running = query(env, {"inspection_number": NUMBER, "refresh": True}, status=202)
    assert running["result"]["refresh_status"] == "running"
    assert env.client.post("/api/execution/v1/task-snapshot-bridge/claim",
                           json={"bridge_id": "other-reader"}, headers=headers).json()["claimed"] is False
    snapshot = {"sample_name": "棉布", "projects": [], "special_wool_occupied_numbers": []}
    response = env.client.post(f"/api/execution/v1/task-snapshot-bridge/{NUMBER}/complete", json={
        "bridge_id": "p4-reader", "claim_token": claimed["claim_token"], "snapshot": snapshot,
    }, headers=headers)
    assert response.status_code == 200, response.text
    final = query(env)
    assert final["result"]["cache_state"] == "ready"
    assert final["result"]["snapshot"]["sample_name"] == "棉布"
    assert final["result"]["snapshot"]["projects"] == []  # Valid empty result, not a missing lookup.
    assert env.db.query(ExecutionRun).count() == 0


@pytest.mark.parametrize("change", [
    {"query_ref": "legacy_fibrecheck.check_record.generic_entry@1"},
    {"query_ref": "https://example.invalid/query"},
    {"query_ref": "legacy_fibrecheck.task_snapshot.get@01"},
    {"connector_version": "9.0.0"}, {"contract_digest": "0" * 64},
    {"input": {}}, {"input": {"inspection_number": NUMBER, "sql": "SELECT 1"}},
    {"input": {"inspection_number": NUMBER, "refresh": "true"}},
])
def test_api_rejects_unknown_or_invalid_contract_without_enqueuing(environment, change):
    env = environment
    payload = {"query_ref": QUERY, "input": {"inspection_number": NUMBER}, **change}
    request(env, "POST", "v1/connector-queries", payload, status=422)
    assert env.db.query(ExecutionTaskSnapshotCache).count() == 0


def test_api_preserves_existing_auth_boundary(environment):
    env = environment
    env.client.cookies.clear()
    request(env, "GET", "v1/connectors/legacy_fibrecheck/capabilities", status=401)
    request(env, "POST", "v1/connector-queries", {"query_ref": QUERY, "input": {"inspection_number": NUMBER}}, status=401)


@pytest.mark.parametrize("state", ["ready", "pending", "stale", "stale_error", "failed"])
def test_native_query_and_api_return_same_cache_without_human_or_external_wait(environment, state):
    env = environment
    if state != "pending":
        task_snapshot(env)
        row = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
        if state != "ready":
            row.expires_at = utcnow() - timedelta(seconds=1)
        if state in {"failed", "stale_error"}:
            row.status, row.error_code = "failed", "legacy_unavailable"
            row.updated_at = utcnow()
        if state == "failed":
            row.snapshot = {}
        env.db.commit()
    # A cached query is allowed at the lowest read-only rollout level.
    settings_before = settings.EXECUTION_V2_ROLLOUT_PROFILE
    settings.EXECUTION_V2_ROLLOUT_PROFILE = "p1_readonly"
    try:
        release = publish(env, stage(env, build_connector_query_smoke_release()))
    finally:
        settings.EXECUTION_V2_ROLLOUT_PROFILE = settings_before
    api = query(env, status=202 if state in {"pending", "stale"} else 200)
    run = request(env, "POST", "v1/runs", {
        "workflow_id": release["workflow_id"], "inspection_number": NUMBER,
        "input_data": {}, "idempotency_key": str(uuid4()),
    }, status=201)["run"]
    drain(env)
    detail = request(env, "GET", f"v1/runs/{run['id']}")
    assert detail["status"] == "completed", detail
    assert detail["output_data"] == api["result"]
    assert detail["output_data"]["cache_state"] == state
    assert env.db.query(ExecutionHumanTask).count() == 0
    assert env.db.query(ExecutionExternalOperation).count() == 0


@pytest.mark.parametrize("change", ["digest", "missing", "operation", "config", "duplicate"])
def test_preflight_requires_exact_read_only_query_dependency(environment, change):
    env = environment
    document = build_connector_query_smoke_release()
    connector = document["dependencies"]["connectors"][0]
    if change == "digest":
        connector["queries"][0]["contract_digest"] = "0" * 64
    elif change == "missing":
        document["dependencies"]["connectors"] = []
    elif change == "operation":
        document["definition"]["nodes"][1]["config"]["query_ref"] = "legacy_fibrecheck.check_record.generic_entry@1"
    elif change == "config":
        document["definition"]["nodes"][1]["config"]["url"] = "https://example.invalid"
    else:
        duplicated = deepcopy(connector["queries"][0])
        duplicated["contract_digest"] = "0" * 64
        connector["queries"].append(duplicated)
    report = request(env, "POST", "v2/workflow-releases/preflight", {"document": _seal(document)})
    assert report["content_valid"] is False, report


def test_worker_claim_requires_query_implementation_not_just_generic_shell(environment):
    env = environment
    registry = get_installed_registry()
    release = publish(env, stage(env, build_connector_query_smoke_release()))
    run = request(env, "POST", "v1/runs", {
        "workflow_id": release["workflow_id"], "inspection_number": NUMBER,
        "input_data": {}, "idempotency_key": str(uuid4()),
    }, status=201)["run"]
    claim = claim_next_node(env.db, worker_id=env.worker.worker_id)
    assert claim.node_id == "start"
    execute_claimed_node(env.db, node_run_id=claim.id, lease_token=claim.lease_token)
    env.db.commit()
    document = deepcopy(env.worker.capability_document)
    document["nodes"] = [value for value in document["nodes"] if value["type"] != "connector.query"]
    document["nodes"].append(registry.executable_binding_for("connector.query", 1))
    document["capability_digest"] = "a" * 64
    record_worker_heartbeat(env.db, worker_id="shell-only", capability_document=document)
    env.db.commit()
    assert claim_next_node(env.db, worker_id="shell-only") is None
    claim = claim_next_node(env.db, worker_id=env.worker.worker_id)
    assert claim.node_id == "query"
    execute_claimed_node(env.db, node_run_id=claim.id, lease_token=claim.lease_token)
    env.db.commit()
    drain(env)
    assert request(env, "GET", f"v1/runs/{run['id']}")["status"] == "completed"


def test_query_implementation_is_part_of_binding_and_prefix_is_unambiguous():
    registry = get_installed_registry()
    query = registry.connectors.resolve_query("legacy_fibrecheck", "*", "task_snapshot.get", 1)
    shell = registry.executable_binding_for("connector.query", 1)
    exact = registry.query_node_binding(shell, query)
    changed = registry.query_node_binding(shell, replace(query, implementation_digest="a" * 64))
    assert exact["execution_binding_digest"] != changed["execution_binding_digest"]
    assert exact["execution_binding_digest"] != shell["execution_binding_digest"]
    with pytest.raises(LookupError):
        registry.handler_for_binding(changed["execution_binding_digest"])
    with pytest.raises(LookupError):
        resolve_connector_reference("legacy.fibrecheck.task_snapshot.get@1", ["legacy", "legacy.fibrecheck"])
