"""Current Worker continues the explicitly compatible d583b9d run snapshot."""
from copy import deepcopy

import pytest

from app.execution.models import ExecutionNodeRun, ExecutionRun
from app.execution.external_operations import complete_external_attempt, record_external_attempt_stage
from app.execution.v2 import binding_compatibility as compatibility
from app.execution.v2.canonical import canonical_sha256
from app.execution.v2.registry import get_installed_registry
from tests.test_execution_connector_operations import operation_env, claim, receipt
from tests.test_execution_native_operations import start
from workflow_native_helpers import environment, drain


@pytest.mark.parametrize("resource", compatibility.RESOURCES)
def test_shipped_previous_bindings_match_exported_registry(resource):
    document = compatibility.load_shipped_binding_compatibility(resource)
    assert document["source_commit"][:7] in resource
    assert all(compatibility.binding_digest(row) == row["execution_binding_digest"] for row in document["bindings"])
    original_capabilities = [{key: row[key] for key in compatibility.CAPABILITY_FIELDS} for row in document["bindings"]]
    assert canonical_sha256(original_capabilities) == document["source_capability_digest"]


@pytest.mark.parametrize("resource", compatibility.RESOURCES)
def test_one_current_worker_advertises_and_executes_all_shipped_previous_bindings(resource):
    registry = get_installed_registry()
    old = compatibility.load_shipped_binding_compatibility(resource)["bindings"]
    current = [spec.execution_binding() for spec in registry.node_specs.all()
               if spec.type not in {"connector.query", "external.operation"}]
    current += registry.query_bindings + registry.operation_bindings
    by_contract = {compatibility.contract_identity(row): row for row in current}
    advertised_rows = [row["execution_binding_digest"] for row in registry.worker_capability_document()["nodes"] if row["ready"]]
    advertised = set(advertised_rows)
    assert len(advertised) == len(advertised_rows)
    for row in old:
        digest = row["execution_binding_digest"]
        assert digest in advertised
        target = by_contract[compatibility.contract_identity(row)]["execution_binding_digest"]
        assert registry.handler_for_binding(digest) == registry.handler_for_binding(target)
    assert {row["connector_operation"]["operation"] for row in old if "connector_operation" in row} >= {
        "original_record.upload", "original_record.review", "check_record.excel_entry", "check_record.generic_entry",
    }
    with pytest.raises(LookupError):
        registry.handler_for_binding("0" * 64)


@pytest.mark.parametrize("connector", [False, True])
def test_contract_changes_never_gain_a_compatibility_alias(monkeypatch, connector):
    registry = deepcopy(get_installed_registry())
    previous = deepcopy(next(row for row in compatibility.load_shipped_binding_compatibility()["bindings"]
                             if bool(row.get("connector_operation")) == connector))
    changed = previous["connector_operation"] if connector else previous
    changed["contract_digest"] = "9" * 64
    previous["execution_binding_digest"] = compatibility.binding_digest(previous)
    monkeypatch.setattr(compatibility, "load_shipped_binding_compatibility", lambda *args: {"bindings": [previous]})
    compatibility.install_shipped_binding_compatibility(registry)
    assert registry.compatibility_bindings == []
    with pytest.raises(LookupError):
        registry.handler_for_binding(previous["execution_binding_digest"])


@pytest.mark.parametrize("resource", compatibility.RESOURCES)
def test_inflight_previous_binding_reaches_registration_and_end_without_rewriting_snapshot(operation_env, resource):
    env = operation_env
    run_info = start(env)
    run = env.db.get(ExecutionRun, run_info["id"])
    lock = deepcopy(run.dependency_lock)
    previous_by_contract = {compatibility.contract_identity(row): row
        for row in compatibility.load_shipped_binding_compatibility(resource)["bindings"]}
    for instance in lock["node_instances"]:
        previous = previous_by_contract[compatibility.contract_identity(instance)]
        for key, value in previous.items():
            if key.startswith("connector_"):
                instance[key] = {**instance[key], **value}
            elif key != "ready":
                instance[key] = value
        node = env.db.query(ExecutionNodeRun).filter_by(run_id=run.id, node_id=instance["node_id"]).one()
        node.execution_binding_digest = previous["execution_binding_digest"]
    run.dependency_lock = lock
    env.db.commit()
    original = deepcopy(run.dependency_lock)
    drain(env)
    env.db.refresh(run)
    assert run.status == "waiting_external"
    operation, attempt, _ = claim(env)
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id="p4-writer", stage="generic_projection_verified")
    env.db.commit()
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id="p4-writer", receipt=receipt(operation))
    env.db.commit()
    drain(env)
    env.db.refresh(run)
    assert run.status == "completed"
    assert run.dependency_lock == original
    assert operation.attempt_count == 1
