"""Native designer and Run races using independent PostgreSQL sessions."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

url=make_url(os.environ['TEST_DATABASE_URL'])
if url.get_backend_name() != 'postgresql' or not (url.database or '').endswith('_test'):
    pytest.skip('requires disposable PostgreSQL TEST_DATABASE_URL ending in _test',allow_module_level=True)

from app.database import SessionLocal
from app.execution.catalog import ensure_default_catalog, ensure_default_rbac
from app.execution.models import ExecutionUser, ExecutionWorkflow, ExecutionRun
from app.execution.errors import ExecutionApiError
from app.execution.engine import create_run
from app.execution.v2.designer import starter_document
from app.execution.v2.drafts import save_draft
from workflow_native_helpers import publish_native_document


def owner():
    with SessionLocal() as db:
        ensure_default_catalog(db);ensure_default_rbac(db)
        actor=ExecutionUser(username='native-pg-'+uuid4().hex,display_name='PG',role='admin',password_hash='unused')
        db.add(actor);db.commit()
        return actor.id


def test_same_revision_only_one_draft_save_commits():
    user_id=owner()
    with SessionLocal() as db:
        doc=starter_document();doc['release']['slug']='draft-'+uuid4().hex
        draft=save_draft(db,document=doc,bindings={},actor=db.get(ExecutionUser,user_id));db.commit()
    gate=Barrier(2)
    def write(index):
        with SessionLocal() as db:
            doc=starter_document();doc['release'].update(slug=draft['document']['release']['slug'],name=f'Edit {index}')
            gate.wait(timeout=10)
            try:
                save_draft(db,document=doc,bindings={},actor=db.get(ExecutionUser,user_id),workflow_id=draft['workflow_id'],revision=draft['revision'])
                db.commit();return 'saved'
            except ExecutionApiError as exc:
                db.rollback();return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,range(2)))
    assert sorted(results)==['draft_revision_changed','saved']
    with SessionLocal() as db:assert db.get(ExecutionWorkflow,draft['workflow_id']).draft_revision==2


def test_simultaneous_identical_run_request_is_one_run(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings,'EXECUTION_CONTRACT_MODE','enforced')
    user_id=owner()
    with SessionLocal() as db:
        doc=starter_document();doc['release']['slug']='run-'+uuid4().hex
        wf=publish_native_document(db,db.get(ExecutionUser,user_id),doc);workflow_id=wf.id
    gate=Barrier(2);key=uuid4().hex
    def submit(_):
        with SessionLocal() as db:
            actor=db.get(ExecutionUser,user_id);wf=db.get(ExecutionWorkflow,workflow_id)
            gate.wait(timeout=10)
            run,duplicate=create_run(db,workflow=wf,actor=actor,inspection_number='PG-001',input_data={},global_data={},idempotency_key=key)
            db.commit();return run.id,duplicate
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
    assert results[0][0]==results[1][0] and sorted(r[1] for r in results)==[False,True]
    with SessionLocal() as db:
        from app.execution.engine import set_run_control_status
        assert db.query(ExecutionRun).filter_by(workflow_id=workflow_id).count()==1
        set_run_control_status(db, run_id=results[0][0], action='cancel', actor=db.get(ExecutionUser,user_id))
        db.commit()


def test_duplicate_batch_dispatch_creates_one_child_and_cancel_propagates(monkeypatch):
    from types import SimpleNamespace
    from app.config import settings
    from app.execution.engine import claim_next_node, execute_claimed_node, set_run_control_status
    from app.execution.models import ExecutionNodeRun
    from app.execution.v2.group_handlers import execute_batch
    from app.execution.worker import ExecutionWorker
    from app.execution.worker_state import record_worker_heartbeat
    from test_execution_run_groups import document
    monkeypatch.setattr(settings, 'EXECUTION_CONTRACT_MODE', 'enforced')
    monkeypatch.setattr(settings, 'EXECUTION_V2_ROLLOUT_PROFILE', 'p2_publish')
    user_id=owner();worker=ExecutionWorker(worker_id='groups-pg-'+uuid4().hex)
    with SessionLocal() as db:
        doc=document();doc['release']['slug']='groups-'+uuid4().hex
        record_worker_heartbeat(db, worker_id=worker.worker_id, capability_document=worker.capability_document)
        actor=db.get(ExecutionUser,user_id);workflow=publish_native_document(db,actor,doc)
        parent,_=create_run(db,workflow=workflow,actor=actor,inspection_number='PG-GROUP',input_data={},global_data={},idempotency_key=uuid4().hex)
        parent_id=parent.id;db.commit()
        for _ in range(3):
            node=claim_next_node(db,worker_id=worker.worker_id)
            assert node is not None
            node_id,token=node.id,node.lease_token;db.commit()
            if node.node_type == 'flow.batch':break
            execute_claimed_node(db,node_id,token);db.commit()
        assert node.node_type == 'flow.batch'
        inputs=next(n['input_mapping'] for n in parent.definition_snapshot['nodes'] if n['id']=='batch')
    gate=Barrier(2)
    def dispatch(_):
        with SessionLocal() as db:
            gate.wait(timeout=10)
            result=execute_batch(SimpleNamespace(db=db,node_run=db.get(ExecutionNodeRun,node_id),lease_token=token,input_data=inputs))
            db.commit();return result.output['groups'][0]['run_id']
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(dispatch,range(2)))
    assert results[0] == results[1]
    with SessionLocal() as db:
        assert db.query(ExecutionRun).filter_by(parent_run_id=parent_id).count()==1
        set_run_control_status(db,run_id=parent_id,action='cancel',actor=db.get(ExecutionUser,user_id));db.commit()
        assert db.get(ExecutionRun,parent_id).status=='cancelled'
        assert db.get(ExecutionRun,results[0]).status=='cancelled'
