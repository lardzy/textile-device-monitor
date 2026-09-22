from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy

import pytest
from openpyxl import Workbook
import xlwt

from app.execution.models import ExecutionNodeRun, ExecutionRun, utcnow
from app.execution.v2.designer import starter_document, compile_document
from app.execution.v2.native_handlers import _workbook_extract_batch
from app.execution.connector_queries import execute_query_node, QueryResult
from workflow_native_helpers import environment, stage, publish, run, drain


def test_batch_xls_xlsx_bad_file_and_content_detection(environment):
    env = environment
    root = env.path / 'paper_fiber_records'
    xls = xlwt.Workbook();sheet = xls.add_sheet('Sheet1');sheet.write(31,22,'１００');sheet.write(31,12,'原始依据');xls.save(str(root/'old.xls'))
    xlsx=Workbook();xlsx.active.title='Sheet1';xlsx.active['W32']=100.5;xlsx.active['M32']='current';xlsx.save(root/'new.xlsx')
    (root/'renamed.xls').write_bytes((root/'new.xlsx').read_bytes())
    (root/'broken.xls').write_bytes(b'not a workbook')
    fields=[{'name':'result','sheet':'Sheet1','cell':'W32','required':True},{'name':'standard','sheet':'Sheet1','cell':'M32'}]
    refs=[{'root_id':'paper_fiber_records','relative_path':name} for name in ['old.xls','new.xlsx','broken.xls','renamed.xls']]
    result=_workbook_extract_batch(SimpleNamespace(db=env.db,node={'config':{'fields':fields}},input_data={'sources':refs}))
    assert result['failed_count']==1 and result['total']==4
    assert [row['format'] for row in result['items']]==['xls','xlsx',None,'xlsx']
    assert result['items'][0]['values']=={'result':'１００','standard':'原始依据'}
    assert result['items'][1]['values']['result']==100.5
    assert result['items'][1]['sha256']==result['items'][3]['sha256']
    assert result['items'][2]['errors']
    # xlrd resources have been released; every file can immediately be replaced.
    for ref in refs: (root/ref['relative_path']).unlink()


def test_query_wait_releases_worker_and_retries_with_same_refresh(environment, monkeypatch):
    import app.execution.connector_queries as queries
    from app.execution.v2.registry import get_installed_registry
    ready={'value':False};calls=[]
    def handler(db,data):
        calls.append(deepcopy(data))
        snapshot = {'schema_version': 5, 'inspection_number': data['inspection_number'], 'projects': [], 'special_wool_occupied_numbers': []}
        return QueryResult({'inspection_number':data['inspection_number'],'cache_state':'ready' if ready['value'] else 'pending','refresh_status':'ready' if ready['value'] else 'queued','snapshot':snapshot if ready['value'] else None,'revision':1,'fetched_at':None,'expires_at':None,'error_code':None}, None if ready['value'] else {'status_url':'test'})
    monkeypatch.setattr(queries,'_task_snapshot',handler)
    get_installed_registry.cache_clear()
    # Refresh capabilities after installing the deterministic query service.
    from app.execution.worker_state import record_worker_heartbeat
    from app.execution.worker import ExecutionWorker
    env=environment;env.worker=ExecutionWorker(worker_id='query-worker');record_worker_heartbeat(env.db,worker_id=env.worker.worker_id,capability_document=env.worker.capability_document);env.db.commit()
    document=starter_document()
    document['definition']['nodes'].insert(1,{'id':'query','name':'查询','type':'connector.query','type_version':2,'config':{'query_ref':'legacy_fibrecheck.task_snapshot.get@1','wait_until_ready':True,'wait_timeout_seconds':30,'retry_interval_seconds':2},'input_mapping':{'inspection_number':'$.inputs.inspection_number','refresh':True}})
    document['definition']['edges']=[{'id':'sq','source':'start','target':'query','join_policy':'all'},{'id':'qe','source':'query','target':'end','join_policy':'all'}]
    compiled=compile_document(document);assert compiled['content_valid'],compiled['issues']
    deployment=publish(env,stage(env,compiled['document']))
    record=run(env,deployment['workflow_id'],inputs={'inspection_number':'26W006824'});drain(env)
    env.db.expire_all();node=env.db.query(ExecutionNodeRun).filter_by(run_id=record['id'],node_id='query').one()
    assert node.status=='ready' and node.attempt_count==1
    drain(env);env.db.refresh(node);assert node.attempt_count==1
    ready['value']=True;node.ready_at=utcnow()-timedelta(seconds=1);env.db.commit();drain(env);env.db.expire_all()
    assert env.db.get(ExecutionRun,record['id']).status=='completed', [(n.node_id,n.status,n.error_code,n.error_message) for n in env.db.query(ExecutionNodeRun).filter_by(run_id=record['id'])]
    assert [data['refresh'] for data in calls]==[True,False]
    get_installed_registry.cache_clear()


def test_query_timeout_is_bounded():
    now=utcnow()
    context=SimpleNamespace(node={'config':{'query_ref':'demo.query@1','wait_until_ready':True,'wait_timeout_seconds':1}},node_run=SimpleNamespace(output_data={'_query_wait_started':(now-timedelta(seconds=2)).isoformat()}),input_data={'refresh':True},db=None)
    query=SimpleNamespace(query_ref='demo.query@1',handler=lambda db,data:QueryResult({}, {'pending':True}))
    from app.execution.errors import ExecutionApiError
    with pytest.raises(ExecutionApiError,match='等待检务查询超时'): execute_query_node(context,query=query)
