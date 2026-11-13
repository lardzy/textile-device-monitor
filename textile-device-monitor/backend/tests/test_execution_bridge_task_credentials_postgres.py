"""Personal credential claims keep one shared Writer slot under PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

url = make_url(os.environ["TEST_DATABASE_URL"])
if url.get_backend_name() != "postgresql" or not url.database.endswith("_test"):
    pytest.skip("requires a disposable PostgreSQL *_test database", allow_module_level=True)

from app.config import settings
from app.database import SessionLocal
from app.execution.external_operations import (
    LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION, _account_scope_key,
    bridge_external_operation, claim_approved_external_operation, fail_external_attempt,
)
from app.execution.models import (
    ExecutionAuditLog, ExecutionCredential, ExecutionExternalAttempt,
    ExecutionExternalOperation, ExecutionUser, utcnow,
)
from app.execution.security import encrypt_credential


@pytest.mark.parametrize("invalid_first", [False, True])
def test_personal_account_claims_share_global_capacity_and_never_mix_credentials(monkeypatch, invalid_first):
    monkeypatch.setattr(settings, "EXECUTION_LEGACY_MICROSCOPY_FINAL_ENTRY_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_CREDENTIAL_KEY", "isolated-personal-claim-postgres-key")
    suffix = uuid4().hex
    operation_ref = "legacy_fibrecheck.check_record.generic_entry@1"
    supported = {operation_ref, LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION}
    operation_ids, credential_ids, user_ids = [], [], []
    accounts = [f"person-{suffix}-{i}" for i in range(2)]
    passwords = [f"isolated-personal-password={i}" for i in range(2)]
    with SessionLocal() as db:
        for index in range(2):
            user = ExecutionUser(username=accounts[index], display_name="隔离验收", role="user", password_hash="unused")
            db.add(user)
            db.flush()
            credential = ExecutionCredential(user_id=user.id, system_key="legacy_inspection",
                account_name=accounts[index], encrypted_secret=("corrupt-test-secret" if invalid_first and index == 0
                else encrypt_credential(passwords[index])), revision=1)
            db.add(credential)
            db.flush()
            operation = ExecutionExternalOperation(
                connector_key="legacy_fibrecheck", operation_key=uuid4().hex,
                created_by_id=user.id, credential_id=credential.id, credential_revision=1,
                account_scope_key=_account_scope_key(accounts[index]), remote_business_key=uuid4().hex,
                status="approved", payload_checksum="d" * 64,
                preflight_expires_at=utcnow() + timedelta(hours=1), approval_expires_at=None,
                created_at=utcnow() + timedelta(seconds=index),
                request_summary={"operation_type": LEGACY_GENERIC_CHECK_RECORD_ENTRY_OPERATION,
                                 "connector_submission": {"operation_ref": operation_ref}},
            )
            db.add(operation)
            db.flush()
            user_ids.append(user.id)
            credential_ids.append(credential.id)
            operation_ids.append(operation.id)
        db.commit()
    barrier = Barrier(2)

    def claim():
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            result = claim_approved_external_operation(db, bridge_id="shared-personal-bridge",
                use_task_credentials=True, supported_operation_types=supported)
            if result is None:
                db.commit()
                return None
            operation, attempt, credential = result
            response = bridge_external_operation(operation, credential=credential, use_task_credentials=True)
            db.commit()
            return response, attempt.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=20) for future in [pool.submit(claim), pool.submit(claim)]]
        assert results.count(None) == 1
        first, attempt_id = next(item for item in results if item is not None)
        index = 1 if invalid_first else 0
        assert first["id"] == operation_ids[index]
        assert first["credential"] == {"account_name": accounts[index], "password": passwords[index]}
        with SessionLocal() as db:
            assert db.query(ExecutionExternalAttempt).filter(
                ExecutionExternalAttempt.operation_id.in_(operation_ids)).count() == 1
            if invalid_first:
                assert db.get(ExecutionExternalOperation, operation_ids[0]).status == "failed"
                assert db.get(ExecutionExternalOperation, operation_ids[0]).attempt_count == 0
                return
            fail_external_attempt(db, attempt_id=attempt_id, bridge_id="shared-personal-bridge",
                stage="generic_save_started", error_code="synthetic_writer_stopped")
            db.commit()
            operation, attempt, credential = claim_approved_external_operation(db,
                bridge_id="shared-personal-bridge", use_task_credentials=True,
                supported_operation_types=supported)
            second = bridge_external_operation(operation, credential=credential, use_task_credentials=True)
            db.commit()
            assert second["id"] == operation_ids[1]
            assert second["credential"] == {"account_name": accounts[1], "password": passwords[1]}
    finally:
        with SessionLocal() as db:
            db.query(ExecutionAuditLog).filter(ExecutionAuditLog.resource_id.in_(operation_ids)).delete(synchronize_session=False)
            db.query(ExecutionExternalAttempt).filter(ExecutionExternalAttempt.operation_id.in_(operation_ids)).delete(synchronize_session=False)
            db.query(ExecutionExternalOperation).filter(ExecutionExternalOperation.id.in_(operation_ids)).delete(synchronize_session=False)
            db.query(ExecutionCredential).filter(ExecutionCredential.id.in_(credential_ids)).delete(synchronize_session=False)
            db.query(ExecutionUser).filter(ExecutionUser.id.in_(user_ids)).delete(synchronize_session=False)
            db.commit()
