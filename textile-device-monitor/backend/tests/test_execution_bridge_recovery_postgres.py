"""PostgreSQL row-lock regressions for stopped-Writer receipt handoff.

Run with an explicit TEST_DATABASE_URL whose database name ends in _test.
All writer receipts are synthetic; no remote system is contacted.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from datetime import timedelta
from uuid import uuid4
import pytest
from app.database import SessionLocal
from app.execution.engine import set_run_control_status
from app.execution.external_operations import recover_external_attempt, fail_external_attempt, BRIDGE_ACTIVE_WRITE_STATUSES
from app.execution.models import ExecutionAuditLog, ExecutionCategory, ExecutionWorkflow, ExecutionRun, ExecutionNodeRun, ExecutionExternalAttempt, ExecutionExternalOperation, ExecutionUser, utcnow
from app.execution.v2.group_handlers import settle_cancelled_batches
from tests.test_execution_late_receipt_postgres import expired_operation  # noqa: F401


def test_duplicate_recovered_success_receipts_complete_once(expired_operation):
    data=expired_operation
    barrier=Barrier(2)
    def recover():
        with SessionLocal() as db:
            barrier.wait(timeout=10)
            result=recover_external_attempt(db,attempt_id=data['attempt_id'],operation_id=data['operation_id'],bridge_id='late-writer',process_exited=True,receipt=data['receipt'])
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=[future.result(timeout=20) for future in [pool.submit(recover),pool.submit(recover)]]
    assert all(item['released'] and item['operation']['status']=='completed' for item in outcomes)
    assert {item['reason'] for item in outcomes}=={'receipt_completed','already_settled'}
    with SessionLocal() as db:
        assert db.query(ExecutionAuditLog).filter_by(resource_id=data['operation_id'],action='external_operation.complete').count()==1
        assert db.query(ExecutionExternalAttempt).filter_by(operation_id=data['operation_id']).count()==1


def _attach_group(data):
    with SessionLocal() as db:
        category=ExecutionCategory(key=uuid4().hex,name='Recovery PG')
        db.add(category);db.flush()
        workflow=ExecutionWorkflow(slug=uuid4().hex,name='Recovery PG',category_id=category.id,draft_definition={})
        db.add(workflow);db.flush()
        common=dict(workflow_id=workflow.id,created_by_id=data['admin_id'],inspection_number=data['number'],definition_snapshot={},definition_checksum='a'*64,contract_checksum='b'*64)
        parent=ExecutionRun(**common,idempotency_key=uuid4().hex,status='running')
        db.add(parent);db.flush()
        child=ExecutionRun(**common,idempotency_key=uuid4().hex,status='waiting_external',parent_run_id=parent.id)
        db.add(child);db.flush()
        node=ExecutionNodeRun(run_id=child.id,node_id='entry',node_type='external.test',node_name='Entry',status='waiting_external')
        db.add(node);db.flush()
        operation=db.get(ExecutionExternalOperation,data['operation_id'])
        operation.run_id,operation.node_run_id=child.id,node.id
        db.commit()
        return parent.id,child.id


@pytest.mark.parametrize('cancel_first',[False,True])
def test_recovery_and_cancel_serialize_and_settle_group(expired_operation,cancel_first):
    data=expired_operation
    parent_id,child_id=_attach_group(data)
    ready=Event()
    def finish(db,cancel):
        if cancel:
            set_run_control_status(db,run_id=parent_id,action='cancel',actor=db.get(ExecutionUser,data['admin_id']))
        else:
            result=recover_external_attempt(db,attempt_id=data['attempt_id'],operation_id=data['operation_id'],bridge_id='late-writer',process_exited=True,error_code='COMException',message='Excel stopped')
            assert result['released']
    def second():
        with SessionLocal() as db:
            ready.set();finish(db,not cancel_first);db.commit()
    with SessionLocal() as first,ThreadPoolExecutor(max_workers=1) as pool:
        finish(first,cancel_first)
        later=pool.submit(second)
        assert ready.wait(timeout=5)
        assert not later.done()
        first.commit();later.result(timeout=20)
    with SessionLocal() as db:
        settle_cancelled_batches(db);db.commit()
        assert db.get(ExecutionRun,child_id).status=='cancelled'
        assert db.get(ExecutionRun,parent_id).status=='cancelled'
        operation=db.get(ExecutionExternalOperation,data['operation_id'])
        assert operation.status=='reconciliation_required'
        assert operation.status not in BRIDGE_ACTIVE_WRITE_STATUSES
        assert operation.verification['stopped_writer_recovery']['remote_write_performed'] is None
        assert operation.attempt_count==1


def test_normal_failure_after_cancel_settles_without_recovery_endpoint(expired_operation):
    data=expired_operation
    parent_id,child_id=_attach_group(data)
    with SessionLocal() as db:
        attempt=db.get(ExecutionExternalAttempt,data['attempt_id'])
        operation=db.get(ExecutionExternalOperation,data['operation_id'])
        attempt.lease_expires_at=operation.lease_expires_at=utcnow()+timedelta(minutes=2)
        db.commit()
        set_run_control_status(db,run_id=parent_id,action='cancel',actor=db.get(ExecutionUser,data['admin_id']))
        db.commit()
        fail_external_attempt(db,attempt_id=attempt.id,bridge_id='late-writer',stage='generic_projection_verified',error_code='COMException',message='Excel exited',diagnostics={'exception_type':'COMException','stack_trace':'full test stack'})
        db.commit()
        settle_cancelled_batches(db);db.commit()
        assert db.get(ExecutionRun,child_id).status==db.get(ExecutionRun,parent_id).status=='cancelled'
        assert operation.status=='reconciliation_required'
        assert db.get(ExecutionRun,child_id).output_data['external_side_effect_unknown'] is True
