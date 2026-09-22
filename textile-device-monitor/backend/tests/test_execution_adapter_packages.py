import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution import adapter_packages
from app.execution.external_operations import claim_approved_external_operation, complete_external_attempt, record_external_attempt_stage, bridge_external_operation
from app.execution.models import ExecutionCredential, ExecutionRun
from app.execution.v2.registry import reset_installed_registry_cache
from workflow_native_helpers import environment, request


@pytest.fixture
def adapter(environment, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EXECUTION_CREDENTIAL_KEY", "isolated-adapter-test-credential-key")
    path = Path(__file__).parents[2] / "adapters/example/textile_example_adapter/__init__.py"
    spec = importlib.util.spec_from_file_location("test_example_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(adapter_packages, "entry_points", lambda **kwargs: [SimpleNamespace(name="example", load=lambda: module.register)])
    adapter_packages.installed_adapters.cache_clear()
    reset_installed_registry_cache()
    try:
        yield environment
    finally:
        adapter_packages.installed_adapters.cache_clear()
        reset_installed_registry_cache()


def test_external_package_query_is_discovered_without_central_dispatch_changes(adapter):
    result = request(adapter, "POST", "v1/connector-queries", {"query_ref": "lab.example.echo@1", "input": {"value": "跨包查询"}})
    assert "跨包查询" in str(result)
    catalog = request(adapter, "GET", "v2/designer/catalog")
    assert any(item["connector_id"] == "lab.example" for item in catalog["connectors"])
    credentials = request(adapter, "GET", "v1/credentials")
    assert any(item["key"] == "lab.example" for item in credentials["systems"])
    stored = request(adapter, "PUT", "v1/credentials/lab.example", {"account_name": "example", "secret": "offline-test-only"})
    assert stored["system_key"] == "lab.example"


def test_external_package_operation_uses_durable_queue_claim_and_receipt(adapter):
    env = adapter
    credential = ExecutionCredential(user_id=env.admin.id, system_key="lab.example", account_name="example", encrypted_secret="unused", revision=1)
    env.db.add(credential)
    env.db.commit()
    payload = {"operation_ref": "lab.example.record.save@1", "credential_id": credential.id, "idempotency_key": "example-one",
               "input": {"inspection_number": "DEMO1", "values": {"result": "示例"}}}
    result = request(env, "POST", "v1/connector-operations", payload, status=202)
    assert env.db.query(ExecutionRun).count() == 0
    assert request(env, "POST", "v1/connector-operations", payload)["operation"]["id"] == result["operation"]["id"]
    operation, attempt, bound_credential = claim_approved_external_operation(env.db, bridge_id="example-bridge", account_name="example", supported_operation_types={"lab.example.record.save@1"})
    machine = bridge_external_operation(operation, credential=bound_credential)["machine_payload"]
    assert machine["values"] == payload["input"]["values"]
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="example-bridge", stage="verified")
    env.db.commit()
    receipt = {"operation_id": operation.id, "payload_checksum": operation.payload_checksum, "values": machine["values"]}
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="example-bridge", receipt=receipt)
    env.db.commit()
    assert request(env, "GET", f"v1/connector-operations/{operation.id}")["status"] == "completed"


def test_package_workflow_uses_same_operation_and_resumes_after_receipt(adapter):
    from copy import deepcopy
    from app.execution.v2.designer import compile_document
    from app.execution.v2.examples import build_connector_operation_smoke_release
    from app.execution.v2.registry import get_installed_registry
    from app.execution.worker import ExecutionWorker
    from app.execution.worker_state import record_worker_heartbeat
    from workflow_native_helpers import publish, run, drain

    env = adapter
    credential = ExecutionCredential(user_id=env.admin.id, system_key="lab.example", account_name="example", encrypted_secret="unused", revision=1)
    env.db.add(credential)
    env.db.flush()
    env.worker = ExecutionWorker(worker_id=env.worker.worker_id)
    record_worker_heartbeat(env.db, worker_id=env.worker.worker_id, capability_document=env.worker.capability_document)
    env.db.commit()
    contract = get_installed_registry().resolve_operation("lab.example", "*", "record.save", 1)
    document = build_connector_operation_smoke_release()
    document["resources"]["credential_slots"][0]["connector_id"] = "lab.example"
    document["definition"]["input_schema"] = deepcopy(contract.spec["input_schema"])
    document["definition"]["output_schema"] = deepcopy(contract.spec["output_schema"])
    start, node, end = document["definition"]["nodes"]
    start["input_mapping"] = {}
    node["config"]["operation_ref"] = contract.operation_ref
    node["input_mapping"] = {key: f"$.inputs.{key}" for key in contract.spec["input_schema"]["properties"]}
    end["input_mapping"] = {key: f"$.nodes.query.output.{key}" for key in contract.spec["output_schema"]["properties"]}
    compiled = compile_document(document)
    assert compiled["content_valid"], compiled["issues"]
    report = request(env, "POST", "v2/workflow-releases/preflight", {"document": compiled["document"]})
    assert report["content_valid"], report
    release = request(env, "POST", "v2/workflow-releases/apply", {"preflight_token": report["preflight_token"]}, status=201)
    request(env, "PUT", f"v2/workflow-releases/{release['id']}/deployment-binding", {
        "environment": "default", "expected_revision": 0,
        "bindings": {"credential_slots": {"legacy": {"credential_id": credential.id, "revision": 1}}},
    })
    published = publish(env, release)
    task = run(env, published["workflow_id"], inputs={"values": {"result": "工作流"}})
    drain(env)
    assert request(env, "GET", f"v1/runs/{task['id']}")["status"] == "waiting_external"
    operation, attempt, _ = claim_approved_external_operation(env.db, bridge_id="example-bridge", account_name="example", supported_operation_types={contract.operation_ref})
    assert operation.run_id == task["id"]
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="example-bridge", stage="verified")
    env.db.commit()
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="example-bridge", receipt={
        "operation_id": operation.id, "payload_checksum": operation.payload_checksum, "values": {"result": "工作流"},
    })
    env.db.commit()
    drain(env)
    assert request(env, "GET", f"v1/runs/{task['id']}")["status"] == "completed"


def test_package_unknown_write_reconciles_without_retry_and_requires_bound_version(adapter, monkeypatch):
    from app.execution.errors import ExecutionApiError
    from app.execution.external_operations import fail_external_attempt
    from app.execution.models import ExecutionExternalOperation

    env = adapter
    credential = ExecutionCredential(user_id=env.admin.id, system_key="lab.example", account_name="example", encrypted_secret="unused", revision=1)
    env.db.add(credential)
    env.db.commit()
    request(env, "POST", "v1/connector-operations", {"operation_ref": "lab.example.record.save@1", "credential_id": credential.id,
        "idempotency_key": "unknown", "input": {"inspection_number": "DEMO1", "values": {"result": 1}}}, status=202)
    operation, attempt, _ = claim_approved_external_operation(env.db, bridge_id="example-bridge", account_name="example", supported_operation_types={"lab.example.record.save@1"})
    fail_external_attempt(env.db, attempt_id=attempt.id, bridge_id="example-bridge", stage="write_started", error_code="connection_lost")
    env.db.commit()
    assert operation.status == "reconciliation_required"
    assert claim_approved_external_operation(env.db, bridge_id="another-bridge", account_name="example", supported_operation_types={"lab.example.record.save@1"}) is None
    result = request(env, "POST", f"v1/connector-operations/{operation.id}/reconcile", {
        "action": "confirm_no_side_effect", "attempt_id": attempt.id, "payload_checksum": operation.payload_checksum,
        "confirmed_sample_number": "DEMO1", "note": "离线测试查无记录", "evidence": {"absent": True}})
    assert result["operation"]["status"] == "failed" and not result["duplicate"]
    assert env.db.query(ExecutionExternalOperation).count() == 1
    monkeypatch.setattr(adapter_packages, "entry_points", lambda **kwargs: [])
    adapter_packages.installed_adapters.cache_clear()
    reset_installed_registry_cache()
    with pytest.raises(ExecutionApiError, match="适配器版本未安装"):
        adapter_packages.bound_adapter(operation)
