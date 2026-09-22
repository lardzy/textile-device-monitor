"""Neutral original upload/review/entry APIs share the real operation queue."""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest
from app.config import settings
from app.execution.connector_original_records import UPLOAD, REVIEW, ENTRY
from app.execution.external_operations import claim_approved_external_operation, complete_external_attempt, record_external_attempt_stage, _operation_stage_profile
from app.execution.models import ExecutionRun, ExecutionProjectRule, ExecutionExternalOperation
from bridge_test_helpers import upload_receipt, review_receipt
from native_io_helpers import paper_file, NUMBER
from tests.test_execution_connector_operations import operation_env, receipt
from workflow_native_helpers import environment, request


def test_upload_review_entry_without_run_or_database_rule(operation_env, monkeypatch):
    env=operation_env
    monkeypatch.setattr(settings,'EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED',True)
    file=paper_file(env,suffix='.xls')
    path=Path(env.roots['paper_fiber_records'].local_path)/file.relative_path
    project=env.payload['input']['project']
    def submit(ref,data,key):
        return request(env,'POST','v1/connector-operations',{'operation_ref':ref,'credential_id':env.credential.id,
            'inspection_number':NUMBER,'input':data,'idempotency_key':key},status=202)['operation']
    def complete(result,ref,builder):
        current=env.db.get(ExecutionExternalOperation,result['id'])
        claim=claim_approved_external_operation(env.db,bridge_id='neutral',account_name='test-operator',
            supported_operation_types={ref,current.request_summary['operation_type']})
        assert claim is not None
        operation,attempt,_=claim
        record_external_attempt_stage(env.db,attempt_id=attempt.id,bridge_id='neutral',stage=_operation_stage_profile(operation)[2])
        env.db.commit()
        complete_external_attempt(env.db,attempt_id=attempt.id,bridge_id='neutral',receipt=builder(operation));env.db.commit()
        return {'operation_id':operation.id,'receipt':deepcopy(operation.receipt)}
    data={'inspection_number':NUMBER,'project':project,'source':{'root_id':'paper_fiber_records','relative_path':file.relative_path},
        'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'business_fields':{'fiber_category':'其他纤维','inspection_item':'自定义项目',
        'review_item':'自定义项目','review_copies':1,'inspection_method':'定量','inspection_copies':1,'file_type':'定量试验'},'inspector':''}
    uploaded=complete(submit(UPLOAD,data,'upload'),UPLOAD,upload_receipt)
    reviewed=complete(submit(REVIEW,{'upload_result':uploaded},'review'),REVIEW,review_receipt)
    entry=submit(ENTRY,{**env.payload['input'],'review_result':reviewed},'entry')
    complete(entry,ENTRY,receipt)
    assert env.db.query(ExecutionExternalOperation).count()==3
    assert env.db.query(ExecutionRun).count()==env.db.query(ExecutionProjectRule).count()==0
