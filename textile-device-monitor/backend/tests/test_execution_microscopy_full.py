"""Isolated portable JSON acceptance: real Worker/UNO/files, simulated remote receipts."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
from app.config import settings
from app.execution.engine import submit_human_task
from app.execution.external_operations import (claim_approved_external_operation, complete_external_attempt,
    record_external_attempt_stage, _operation_stage_profile, MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES)
from app.execution.models import ExecutionStorageRoot, ExecutionTaskSnapshotCache, ExecutionExternalOperation, ExecutionRun, ExecutionHumanTask, ExecutionNodeRun, ExecutionProjectRule
from bridge_test_helpers import upload_receipt, review_receipt
from native_io_helpers import NUMBER, image_files
from workflow_native_helpers import environment, publish_native_document, run, drain
from tests.test_execution_connector_operations import operation_env
from test_execution_microscopy_json import document


def excel_receipt(operation):
    s=operation.request_summary;file=s['files'][0];package=s['final_entry_package']
    return {'schema_version':1,'receipt_type':s['operation_type'],'operation_id':operation.id,
        'payload_checksum':operation.payload_checksum,'target_sample_number':NUMBER,
        'source_artifact':{k:file[k] for k in ('artifact_id','filename','size_bytes','content_sha256')},
        'task_project':s['task_project'],'template_binding':s['template_binding'],
        'final_entry':{'package_schema_version':5,'expected_existing_register_count':package['expected_existing_register_count'],
            'resulting_register_count':package['expected_existing_register_count']+1,'key_result_count':package['excel_record']['key_result_count'],
            'record_id':'sha256:'+'4'*16,'original_data_filename':'12345678-1234-1234-1234-123456789abc.xls',
            'content_sha256':file['content_sha256'],'proofed':True},
        'controlled_test_override':None,'existing_record_decision':None,
        'stages':list(MICROSCOPY_CHECK_RECORD_ENTRY_ATTEMPT_STAGES),'reconciliation_required':False}


@pytest.mark.skipif(os.getenv('EXECUTION_RUN_UNO_INTEGRATION_TESTS')!='1',reason='Run in the Worker image with UNO')
@pytest.mark.parametrize('cancel',[False,True])
@pytest.mark.parametrize('source', ['bundled', 'exported'])
def test_portable_microscopy_full_chain_or_cancel(operation_env,monkeypatch,cancel,source):
    env=operation_env
    monkeypatch.setattr(settings,'EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED',True)
    image_files(env,1)
    cache=env.db.get(ExecutionTaskSnapshotCache,NUMBER);snapshot=deepcopy(cache.snapshot)
    project=snapshot['projects'][0];project.update(check_item_no='5103.5',check_item_name='纤维微观形貌',check_method='GB/T 36422-2018',seq_num=1,sample_identify=None,give_judgement=0)
    project['project_key']='task-project:'+hashlib.sha256('\0'.join(str(project[k]) for k in ('task_check_item_id','check_item_id','check_item_no','check_item_name','check_method','seq_num')).encode()).hexdigest()[:24]
    cache.snapshot=snapshot
    template_dir=Path(__file__).parents[1]/'app/execution/templates'
    for file in template_dir.glob('*.xls'):shutil.copyfile(file,Path(env.roots['execution_templates'].local_path)/file.name)
    root=ExecutionStorageRoot(root_id='report_upload_images',name='Share',local_path=str(env.path/'share'),source_uri=r'\\server\share',access_mode='write',is_available=True,is_active=True)
    Path(root.local_path).mkdir();env.db.add(root);env.db.commit();env.roots[root.root_id]=root
    doc = document() if source == 'bundled' else json.loads((Path(__file__).parents[2]/'docs/execution-v2/examples/fiber-microscopy-v2.json').read_text())
    # Source snapshots are local fixtures; refresh scheduling is independently covered.
    next(n for n in doc['definition']['nodes'] if n['id']=='task')['input_mapping']['refresh']=False
    rules=next(n for n in doc['definition']['nodes'] if n['id']=='payload')['input_mapping']['rules']
    destination=Path(root.local_path)/rules['target_directory']/NUMBER/(NUMBER+'.png')
    if cancel:
        destination.parent.mkdir(parents=True);destination.write_bytes(b'existing different content')
    workflow=publish_native_document(env.db,env.admin,doc,{
        'root_slots':{slot['slot_id']:{'root_id':slot['slot_id'],'revision':1} for slot in doc['resources']['root_slots']},
        'credential_slots':{'inspection':{'credential_id':env.credential.id,'revision':1}}})
    record=run(env,workflow.id,inputs={'inspection_number':NUMBER,'relative_directory':''});drain(env);env.db.expire_all()
    if cancel:
        task=env.db.query(ExecutionHumanTask).filter_by(run_id=record['id'],status='open').one()
        submit_human_task(env.db,task_id=task.id,actor=env.admin,expected_revision=task.revision,data={'decision':'cancel'})
        env.db.commit();drain(env)
    else:
        for ref,builder in [('legacy_fibrecheck.original_record.upload@1',upload_receipt),('legacy_fibrecheck.original_record.review@1',review_receipt),('legacy_fibrecheck.check_record.excel_entry@1',excel_receipt)]:
            env.db.expire_all();pending=env.db.query(ExecutionExternalOperation).filter_by(status='approved').one()
            operation,attempt,_=claim_approved_external_operation(env.db,bridge_id='test',account_name='test-operator',supported_operation_types={ref,pending.request_summary['operation_type']})
            record_external_attempt_stage(env.db,attempt_id=attempt.id,bridge_id='test',stage=_operation_stage_profile(operation)[2]);env.db.commit()
            complete_external_attempt(env.db,attempt_id=attempt.id,bridge_id='test',receipt=builder(operation));env.db.commit();drain(env)
    env.db.expire_all();current=env.db.get(ExecutionRun,record['id'])
    assert current.status=='completed',[(n.node_id,n.status,n.error_message) for n in env.db.query(ExecutionNodeRun).filter_by(run_id=current.id)]
    assert current.output_data['submitted'] is not cancel
    assert env.db.query(ExecutionExternalOperation).count()==(0 if cancel else 3)
    assert env.db.query(ExecutionProjectRule).count()==0
    assert destination.exists()
    if cancel:assert destination.read_bytes()==b'existing different content'
