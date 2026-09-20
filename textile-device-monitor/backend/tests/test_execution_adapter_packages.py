import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution import adapter_packages
from app.execution.external_operations import claim_approved_external_operation, complete_external_attempt, record_external_attempt_stage, bridge_external_operation
from app.execution.models import ExecutionCredential, ExecutionRun
from app.execution.v2.registry import reset_installed_registry_cache
from tests.test_execution_workflow_replacement import environment, request


@pytest.fixture
def adapter(environment, monkeypatch):
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
