"""One Bridge executes each submitted operation with its owner's credentials."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest

from app.config import settings
from app.execution.models import (
    ExecutionAuditLog, ExecutionCredential, ExecutionEvent, ExecutionExternalAttempt,
    ExecutionExternalOperation, ExecutionTaskSnapshotCache, utcnow,
)
from native_io_helpers import NUMBER
from tests.test_execution_connector_operations import operation_env, receipt, submit
from workflow_native_helpers import environment


BRIDGE_PATH = "/api/execution/v1/external-bridge/claim"
BRIDGE_KEY = "task-credential-bridge-test-key"
OPERATIONS = ["legacy_generic_check_record_entry", "legacy_fibrecheck.check_record.generic_entry@2"]
FIRST_PASSWORD = "first-person-only=密码"
SECOND_PASSWORD = "second-person-only=口令"


@pytest.fixture
def personal_bridge(operation_env, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_BRIDGE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_BRIDGE_TOKEN", BRIDGE_KEY)
    monkeypatch.setattr(settings, "EXECUTION_CREDENTIAL_KEY", "task-credential-isolated-encryption-key")
    env = operation_env
    response = env.client.put("/api/execution/v1/credentials/legacy_inspection", json={
        "account_name": "first-person", "secret": FIRST_PASSWORD,
    })
    assert response.status_code == 200, response.text
    env.db.expire_all()
    return env


def bridge_claim(env, *, expected=200, **changes):
    payload = {"bridge_id": "shared-bridge", "use_task_credentials": True,
               "supported_operation_types": OPERATIONS, **changes}
    response = env.client.post(BRIDGE_PATH, json=payload,
                               headers={"X-Execution-Bridge-Key": BRIDGE_KEY})
    assert response.status_code == expected, response.text
    env.db.expire_all()
    return response.json()


def submit_second_owner(env):
    response = env.user_client.put("/api/execution/v1/credentials/legacy_inspection", json={
        "account_name": "second-person", "secret": SECOND_PASSWORD,
    })
    assert response.status_code == 200, response.text
    credential_id = response.json()["id"]
    number = "26W006702"
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    env.db.add(ExecutionTaskSnapshotCache(
        inspection_number=number, status="ready",
        snapshot={**deepcopy(cache.snapshot), "inspection_number": number},
        fetched_at=utcnow(), expires_at=utcnow() + timedelta(minutes=15),
    ))
    env.db.commit()
    response = env.user_client.post("/api/execution/v1/connector-operations", json={
        **env.payload, "credential_id": credential_id, "idempotency_key": "second-owner",
        "input": {**env.payload["input"], "inspection_number": number},
    })
    assert response.status_code == 202, response.text
    return response.json()["operation"]


def test_same_bridge_claims_two_owners_with_their_personal_passwords(personal_bridge):
    env = personal_bridge
    first = submit(env)["operation"]
    second = submit_second_owner(env)
    claimed = bridge_claim(env)
    assert claimed["operation"]["id"] == first["id"]
    assert claimed["operation"]["credential"] == {
        "account_name": "first-person", "password": FIRST_PASSWORD,
    }
    # The existing single-writer capacity is shared across all personal accounts.
    assert bridge_claim(env) == {"claimed": False}
    attempt_id = claimed["attempt"]["id"]
    headers = {"X-Execution-Bridge-Key": BRIDGE_KEY}
    response = env.client.post(f"/api/execution/v1/external-bridge/attempts/{attempt_id}/stage",
        headers=headers, json={"bridge_id": "shared-bridge", "stage": "generic_projection_verified"})
    assert response.status_code == 200, response.text
    operation = env.db.get(ExecutionExternalOperation, first["id"])
    response = env.client.post(f"/api/execution/v1/external-bridge/attempts/{attempt_id}/complete",
        headers=headers, json={"bridge_id": "shared-bridge", "receipt": receipt(operation)})
    assert response.status_code == 200, response.text
    claimed = bridge_claim(env)
    assert claimed["operation"]["id"] == second["id"]
    assert claimed["operation"]["credential"] == {
        "account_name": "second-person", "password": SECOND_PASSWORD,
    }

    responses = [
        env.client.get("/api/execution/v1/credentials"),
        env.user_client.get("/api/execution/v1/credentials"),
        env.client.get(f"/api/execution/v1/connector-operations/{first['id']}"),
        env.user_client.get(f"/api/execution/v1/connector-operations/{second['id']}"),
        env.client.get(f"/api/execution/v1/external-operations/{first['id']}"),
    ]
    assert all(response.status_code == 200 for response in responses)
    public_responses = [response.json() for response in responses]
    persisted = []
    for model in (ExecutionExternalOperation, ExecutionExternalAttempt, ExecutionAuditLog, ExecutionEvent):
        persisted.extend({column.name: getattr(row, column.name) for column in model.__table__.columns}
                         for row in env.db.query(model).all())
    exposed = json.dumps([public_responses, persisted], ensure_ascii=False, default=str)
    for secret in (FIRST_PASSWORD, SECOND_PASSWORD):
        assert secret not in exposed
    assert "encrypted_secret" not in json.dumps(public_responses)


def test_legacy_bridge_keeps_account_filter_and_receives_no_password(personal_bridge):
    env = personal_bridge
    first = submit(env)["operation"]
    second = submit_second_owner(env)
    assert bridge_claim(env, use_task_credentials=False, account_name="other-account") == {"claimed": False}
    claimed = bridge_claim(env, use_task_credentials=False, account_name="second-person")
    assert claimed["operation"]["id"] == second["id"]
    assert claimed["operation"]["credential"] == {"account_name": "second-person"}
    assert env.db.get(ExecutionExternalOperation, first["id"]).status == "approved"
    bridge_claim(env, use_task_credentials=False, expected=422)


def test_response_error_rolls_back_claim_without_orphan_attempt(personal_bridge, monkeypatch):
    from app.execution import security
    from app.execution.errors import ExecutionApiError

    env = personal_bridge
    submitted = submit(env)["operation"]
    decrypt = security.decrypt_credential
    calls = 0

    def fail_during_response(credential):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ExecutionApiError(500, "credential_decryption_failed", "测试响应构造失败")
        return decrypt(credential)

    monkeypatch.setattr(security, "decrypt_credential", fail_during_response)
    failure = bridge_claim(env, expected=500)
    assert failure["code"] == "credential_decryption_failed"
    operation = env.db.get(ExecutionExternalOperation, submitted["id"])
    assert operation.status == "approved" and operation.attempt_count == 0
    assert operation.lease_owner is None
    assert env.db.query(ExecutionExternalAttempt).count() == 0
    assert env.db.query(ExecutionAuditLog).filter_by(action="external_operation.claim").count() == 0
    monkeypatch.setattr(security, "decrypt_credential", decrypt)
    assert bridge_claim(env)["operation"]["credential"]["password"] == FIRST_PASSWORD


@pytest.mark.parametrize("change", ["owner", "revision", "disabled", "corrupt", "empty", "missing"])
def test_only_invalid_task_is_failed_without_attempt_or_shared_queue_block(personal_bridge, change):
    env = personal_bridge
    submitted = submit(env)["operation"]
    credential = env.db.get(ExecutionCredential, env.credential.id)
    if change == "owner":
        credential.user_id = "not-the-task-owner"
    elif change == "revision":
        credential.revision += 1
    elif change == "corrupt":
        credential.encrypted_secret = "corrupt-test-secret"
    elif change == "empty":
        from app.execution.security import encrypt_credential

        credential.encrypted_secret = encrypt_credential("")
    elif change == "missing":
        env.db.delete(credential)
    else:
        credential.is_active = False
    env.db.commit()
    assert bridge_claim(env) == {"claimed": False}
    operation = env.db.get(ExecutionExternalOperation, submitted["id"])
    assert operation.status == "failed" and operation.attempt_count == 0
    expected_code = {"corrupt": "credential_decryption_failed", "empty": "task_credentials_missing"}.get(change, "external_operation_credential_changed")
    assert operation.error_code == expected_code
    response = env.client.get(f"/api/execution/v1/connector-operations/{operation.id}")
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert response.json()["remote_write_performed"] is False
    assert env.db.query(ExecutionExternalAttempt).count() == 0
    assert env.db.query(ExecutionAuditLog).filter_by(action="external_operation.credential_failed").count() == 1


@pytest.mark.parametrize("corrupt", [False, True])
def test_invalid_first_owner_does_not_block_valid_second_owner(personal_bridge, corrupt):
    env = personal_bridge
    first = submit(env)["operation"]
    second = submit_second_owner(env)
    credential = env.db.get(ExecutionCredential, env.credential.id)
    if corrupt:
        credential.encrypted_secret = "corrupt-test-secret"
    else:
        credential.is_active = False
    env.db.commit()
    claimed = bridge_claim(env)
    assert claimed["operation"]["id"] == second["id"]
    assert claimed["operation"]["credential"] == {"account_name": "second-person", "password": SECOND_PASSWORD}
    assert env.db.get(ExecutionExternalOperation, first["id"]).status == "failed"
    assert env.db.query(ExecutionExternalAttempt).count() == 1
    assert env.db.query(ExecutionExternalAttempt).filter_by(operation_id=first["id"]).count() == 0


def test_invalid_personal_credential_also_finishes_workflow_node(personal_bridge):
    from uuid import uuid4
    from app.execution.models import ExecutionCategory, ExecutionWorkflow, ExecutionRun, ExecutionNodeRun

    env = personal_bridge
    submitted = submit(env)["operation"]
    category = env.db.query(ExecutionCategory).first()
    workflow = ExecutionWorkflow(slug="credential-run-test", name="个人凭据", category_id=category.id, draft_definition={})
    env.db.add(workflow)
    env.db.flush()
    run = ExecutionRun(workflow_id=workflow.id, created_by_id=env.admin.id, inspection_number=NUMBER,
        definition_snapshot={}, definition_checksum="a" * 64, contract_checksum="b" * 64,
        idempotency_key=uuid4().hex, status="waiting_external")
    env.db.add(run)
    env.db.flush()
    node = ExecutionNodeRun(run_id=run.id, node_id="entry", node_type="external.operation",
        node_name="登记", status="waiting_external")
    env.db.add(node)
    env.db.flush()
    operation = env.db.get(ExecutionExternalOperation, submitted["id"])
    operation.run_id, operation.node_run_id = run.id, node.id
    env.credential.is_active = False
    env.db.commit()
    assert bridge_claim(env) == {"claimed": False}
    assert operation.status == node.status == run.status == "failed"
    assert node.error_code == "external_operation_credential_changed"
    assert env.db.query(ExecutionExternalAttempt).count() == 0


def test_bridge_reported_missing_credentials_is_not_automatically_retried(personal_bridge):
    env = personal_bridge
    submit(env)
    claimed = bridge_claim(env)
    attempt_id = claimed["attempt"]["id"]
    response = env.client.post(f"/api/execution/v1/external-bridge/attempts/{attempt_id}/fail",
        headers={"X-Execution-Bridge-Key": BRIDGE_KEY}, json={
            "bridge_id": "shared-bridge", "stage": "authenticated",
            "error_code": "task_credentials_missing", "message": "请配置个人检务凭据",
        })
    assert response.status_code == 200, response.text
    assert response.json()["operation"]["status"] == "failed"
    assert bridge_claim(env) == {"claimed": False}


def test_task_credential_claim_requires_bridge_token(personal_bridge):
    env = personal_bridge
    submit(env)
    response = env.client.post(BRIDGE_PATH, json={
        "bridge_id": "shared-bridge", "use_task_credentials": True,
        "supported_operation_types": OPERATIONS,
    })
    assert response.status_code == 401
    assert FIRST_PASSWORD not in response.text
    assert env.db.query(ExecutionExternalAttempt).count() == 0
