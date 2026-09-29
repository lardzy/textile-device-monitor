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


@pytest.mark.parametrize('generated', [False, True])
def test_upload_review_entry_without_run_or_database_rule(operation_env, monkeypatch, generated):
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
        if ref == UPLOAD:
            from app.execution.external_operations import bridge_external_operation
            # Exercise the actual public -> Bridge transport; the old view
            # dropped file_type although the submitted operation retained it.
            view = bridge_external_operation(current, credential=env.credential)
            for fields in (result['request_summary']['business_fields'],
                           view['request_summary']['business_fields']):
                assert {key: fields[key] for key in data['business_fields']} == data['business_fields']
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
    if generated:
        from app.execution.models import ExecutionArtifact
        env.db.add(ExecutionArtifact(storage_root_id=env.roots['paper_fiber_records'].id,
            relative_path=file.relative_path, filename=NUMBER+'-薄膜正面-微观形貌-原始记录.xls', role='working',
            content_sha256=data['sha256'], size_bytes=path.stat().st_size, immutable=True))
        env.db.commit()
    result = submit(UPLOAD,data,'upload')
    expected_name = '薄膜正面-微观形貌-原始记录.xls' if generated else path.name
    assert result['request_summary']['target_filename'] == NUMBER+'-'+expected_name
    assert result['request_summary']['files'][0]['filename'] == expected_name
    assert result['request_summary']['files'][0]['relative_path'] == file.relative_path
    uploaded=complete(result,UPLOAD,upload_receipt)
    reviewed=complete(submit(REVIEW,{'upload_result':uploaded},'review'),REVIEW,review_receipt)
    entry=submit(ENTRY,{**env.payload['input'],'review_result':reviewed},'entry')
    complete(entry,ENTRY,receipt)
    assert env.db.query(ExecutionExternalOperation).count()==3
    assert env.db.query(ExecutionRun).count()==env.db.query(ExecutionProjectRule).count()==0


def test_neutral_excel_entry_shares_queue_exact_capability_and_receipt(operation_env):
    from app.execution.connector_original_records import EXCEL_ENTRY
    from app.execution.external_operations import (bridge_external_operation,
        MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES)
    from bridge_test_helpers import _BRIDGE_MODULE
    env = operation_env
    file = paper_file(env, suffix='.xls')
    path = Path(env.roots['paper_fiber_records'].local_path) / file.relative_path
    data = {key: deepcopy(env.payload['input'][key]) for key in ('inspection_number', 'project', 'expected_existing_register_count')}
    data.update(source={'root_id': 'paper_fiber_records', 'relative_path': file.relative_path},
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        template={'template_name': '可配置的采集模板', 'mapping_config_sha256': 'b' * 64},
        expected_key_identities=['自定义1', '自定义2'],
        register={'level': '一级', 'sample_identity': '', 'equipment_no': 'ABC-1', 'check_basis': '自定义依据'})
    payload = {'operation_ref': EXCEL_ENTRY, 'credential_id': env.credential.id,
               'input': data, 'idempotency_key': 'excel-entry'}
    result = request(env, 'POST', 'v1/connector-operations', payload, status=202)
    operation = env.db.get(ExecutionExternalOperation, result['operation']['id'])
    kind = operation.request_summary['operation_type']
    assert claim_approved_external_operation(env.db, bridge_id='old', account_name='test-operator',
        supported_operation_types={kind}) is None
    claimed, attempt, credential = claim_approved_external_operation(env.db, bridge_id='new', account_name='test-operator',
        supported_operation_types={kind, EXCEL_ENTRY})
    view = bridge_external_operation(claimed, credential=credential)
    package, source = _BRIDGE_MODULE.validate_final_entry_machine_payload(view, view['request_summary'])
    assert package['schema_version'] == 5 and package['excel_record']['key_result_count'] == 2
    assert package['excel_record']['register']['equipment_no'] == 'ABC-1'
    record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id='new', stage='excel_proof_verified')
    env.db.commit()
    summary = operation.request_summary
    receipt = {'schema_version': 1, 'receipt_type': kind, 'operation_id': operation.id,
        'payload_checksum': operation.payload_checksum, 'target_sample_number': NUMBER,
        'source_artifact': {key: source[key] for key in ('artifact_id', 'filename', 'size_bytes', 'content_sha256')},
        'task_project': data['project'], 'template_binding': data['template'],
        'final_entry': {'package_schema_version': 5, 'expected_existing_register_count': 0,
            'resulting_register_count': 1, 'key_result_count': 2, 'record_id': 'sha256:' + '3' * 16,
            'original_data_filename': '12345678-1234-1234-1234-123456789abc.xls', 'content_sha256': data['sha256'], 'proofed': True},
        'controlled_test_override': None, 'existing_record_decision': None,
        'stages': list(MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES), 'reconciliation_required': False}
    from app.execution.external_operations import _validate_final_entry_reconciliation_evidence
    from app.execution.errors import ExecutionApiError
    assert _validate_final_entry_reconciliation_evidence(operation, attempt=attempt,
        action='confirm_completed', evidence={'receipt': receipt}) == receipt
    with pytest.raises(ExecutionApiError):
        _validate_final_entry_reconciliation_evidence(operation, attempt=attempt,
            action='confirm_completed', evidence={'actual_register_count': 1})
    complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id='new', receipt=receipt)
    env.db.commit()
    repeated = request(env, 'POST', 'v1/connector-operations', payload)
    assert repeated['duplicate'] and repeated['operation']['status'] == 'completed'
    assert env.db.query(ExecutionExternalOperation).count() == 1
    assert env.db.query(ExecutionRun).count() == env.db.query(ExecutionProjectRule).count() == 0


def test_excel_source_is_reverified_before_claim(operation_env):
    from app.execution.connector_original_records import EXCEL_ENTRY
    from app.execution.errors import ExecutionApiError
    env = operation_env
    file = paper_file(env, suffix='.xls')
    path = Path(env.roots['paper_fiber_records'].local_path) / file.relative_path
    data = {key: deepcopy(env.payload['input'][key]) for key in ('inspection_number', 'project', 'expected_existing_register_count')}
    data.update(source={'root_id': 'paper_fiber_records', 'relative_path': file.relative_path},
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        template={'template_name': '任意模板', 'mapping_config_sha256': 'b' * 64}, expected_key_identities=[''],
        register={key: '' for key in ('level', 'sample_identity', 'equipment_no', 'check_basis')})
    request(env, 'POST', 'v1/connector-operations', {'operation_ref': EXCEL_ENTRY,
        'credential_id': env.credential.id, 'input': data, 'idempotency_key': 'changed-excel'}, status=202)
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ExecutionApiError) as error:
        claim_approved_external_operation(env.db, bridge_id='new', account_name='test-operator',
            supported_operation_types={'legacy_microscopy_check_record_entry', EXCEL_ENTRY})
    assert error.value.code == 'connector_source_changed'
