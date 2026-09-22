"""Generic operation nodes share submissions, exact scheduling and recovery."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from app.execution.connector_recovery import recover_connector_updates
from app.execution.engine import claim_next_node, execute_claimed_node
from app.execution.external_operations import complete_external_attempt, fail_external_attempt, record_external_attempt_stage
from app.execution.models import ExecutionExternalOperation, ExecutionNodeRun, ExecutionTaskSnapshotCache, utcnow
from app.execution.v2.examples import build_connector_operation_smoke_release, _seal
from app.execution.v2.registry import get_installed_registry
from app.execution.worker_state import record_worker_heartbeat
from tests.test_execution_connector_operations import operation_env, claim, receipt
from tests.test_execution_connector_updates import update_env, claim_update, after_record
from tests.test_execution_domain_services import NUMBER
from workflow_native_helpers import environment, request, stage, publish, drain


def start(env):
    document = build_connector_operation_smoke_release(env.payload["operation_ref"].split(".", 1)[1].split("@")[0])
    report = request(env, "POST", "v2/workflow-releases/preflight", {"document": document})
    assert report["content_valid"], report
    release = request(env, "POST", "v2/workflow-releases/apply", {"preflight_token": report["preflight_token"]}, status=201)
    request(env, "PUT", f"v2/workflow-releases/{release['id']}/deployment-binding", {
        "environment": "default", "expected_revision": 0,
        "bindings": {"credential_slots": {"legacy": {"credential_id": env.credential.id, "revision": 1}}},
    })
    release = publish(env, release)
    return request(env, "POST", "v1/runs", {
        "workflow_id": release["workflow_id"], "inspection_number": NUMBER,
        "input_data": env.payload["input"], "idempotency_key": str(uuid4()),
    }, status=201)["run"]


def test_operation_node_uses_same_writer_receipt_and_resumes_dag(operation_env):
    env = operation_env
    run = start(env)
    drain(env)
    detail = request(env, "GET", f"v1/runs/{run['id']}")
    assert detail["status"] == "waiting_external", [(n.node_id, n.error_code, n.error_message) for n in env.db.query(ExecutionNodeRun).filter_by(run_id=run["id"]).all()]
    operation, attempt, _ = claim(env)
    assert operation.run_id == run["id"]
    assert operation.request_summary["connector_submission"]["input"] == env.payload["input"]
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_projection_verified")
    env.db.commit()
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=receipt(operation))
    env.db.commit()
    drain(env)
    detail = request(env, "GET", f"v1/runs/{run['id']}")
    assert detail["status"] == "completed", detail
    assert detail["output_data"] == {"operation_id": operation.id, "status": "completed",
                                     "receipt": receipt(operation), "remote_write_performed": True}


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("written", [False, True])
def test_update_readback_resumes_or_settles_cancelled_dag(update_env, cancel, written):
    env = update_env
    run = start(env)
    drain(env)
    operation, attempt, _ = claim_update(env)
    if cancel:
        request(env, "POST", f"v1/runs/{run['id']}/cancel", {})
    fail_external_attempt(env.db, attempt_id=attempt.id, bridge_id="update-writer", stage="update_started", error_code="connection_lost")
    env.db.commit()
    assert recover_connector_updates(env.db) == 1
    env.db.commit()
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    cache.status, cache.fetched_at = "ready", utcnow()
    cache.check_records = {"schema_version": 1, "records": [after_record(env) if written else env.before]}
    cache.revision += 1
    env.db.commit()
    assert recover_connector_updates(env.db) == 1
    env.db.commit()
    drain(env)
    detail = request(env, "GET", f"v1/runs/{run['id']}")
    assert detail["status"] == ("cancelled" if cancel else "completed" if written else "waiting_external"), detail
    assert operation.status == ("completed" if written else "cancelled" if cancel else "approved")
    assert env.db.query(ExecutionExternalOperation).count() == 1


def test_shell_only_worker_cannot_claim_operation(operation_env):
    env = operation_env
    start(env)
    node = claim_next_node(env.db, worker_id=env.worker.worker_id)
    execute_claimed_node(env.db, node_run_id=node.id, lease_token=node.lease_token)
    env.db.commit()
    registry = get_installed_registry()
    capability = deepcopy(env.worker.capability_document)
    capability["nodes"] = [n for n in capability["nodes"] if n["type"] != "external.operation"]
    capability["nodes"].append(registry.executable_binding_for("external.operation", 1))
    capability["capability_digest"] = "a" * 64
    record_worker_heartbeat(env.db, worker_id="operation-shell", capability_document=capability)
    env.db.commit()
    assert claim_next_node(env.db, worker_id="operation-shell") is None
    assert claim_next_node(env.db, worker_id=env.worker.worker_id).node_id == "query"


@pytest.mark.parametrize("change", ["missing", "digest", "duplicate", "unavailable"])
def test_preflight_requires_exact_operation_contract(operation_env, change):
    document = build_connector_operation_smoke_release()
    operations = document["dependencies"]["connectors"][0]["operations"]
    if change == "missing":
        document["dependencies"]["connectors"] = []
    elif change == "digest":
        operations[0]["contract_digest"] = "0" * 64
    elif change == "duplicate":
        operations.append(deepcopy(operations[0]))
        operations[-1]["contract_digest"] = "0" * 64
    else:
        document["definition"]["nodes"][1]["config"]["operation_ref"] = "legacy_fibrecheck.unknown@1"
    report = request(operation_env, "POST", "v2/workflow-releases/preflight", {"document": _seal(document)})
    assert not report["content_valid"], report


def test_operation_implementation_changes_binding():
    registry = get_installed_registry()
    operation = registry.resolve_operation("legacy_fibrecheck", "*", "check_record.generic_entry", 2)
    shell = registry.executable_binding_for("external.operation", 1)
    binding = registry.operation_node_binding(shell, operation)
    changed = registry.operation_node_binding(shell, replace(operation, implementation_digest="a" * 64))
    assert binding["execution_binding_digest"] != changed["execution_binding_digest"]
    assert binding["execution_binding_digest"] != shell["execution_binding_digest"]
