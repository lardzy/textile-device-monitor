"""Sequential groups use ordinary immutable Runs and retries, including cancellation."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.execution.engine import (claim_next_node, fail_node, retry_failed_node,
    set_run_control_status, submit_human_task)
from app.execution.errors import ExecutionApiError
from app.execution.models import ExecutionRun, ExecutionNodeRun, ExecutionHumanTask, utcnow
from app.execution.v2.designer import starter_document
from app.execution.v2.group_handlers import normalize_groups, child_summaries
from workflow_native_helpers import environment, publish_native_document, run, drain


def document():
    doc = starter_document()
    doc['release']['slug'] = 'group-test'
    doc['definition']['input_schema']['properties']['group'] = {'type': 'string', 'default': ''}
    def node(id, type, config=None, mapping=None, version=1):
        return {'id': id, 'name': id, 'type': type, 'type_version': version, 'config': config or {}, 'input_mapping': mapping or {}}
    doc['definition']['nodes'] = [doc['definition']['nodes'][0],
        node('route', 'flow.branch', {'expression_version': 1, 'multi_match': 'all'}),
        node('batch', 'flow.batch', mapping={'items': [{'key': k, 'label': k, 'inputs': {'group': k}} for k in ['a', 'b', 'c']]}),
        node('form', 'human.form', {'title': 'Confirm', 'auto_submit_complete': False,
             'result_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}},
             {'defaults': {}, 'context': {}, 'form_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}}, 2),
        node('child_end', 'core.end', mapping={'result': '$.nodes.form.output', 'group': '$.inputs.group'}, version=2),
        node('end', 'core.end', mapping={'groups': '$.nodes.batch.output.groups', 'count': '$.nodes.batch.output.count'}, version=2)]
    doc['definition']['edges'] = [
        {'id': a+'-'+b, 'source': a, 'target': b, 'join_policy': 'all', **extra}
        for a,b,extra in [('start','route',{}), ('route','form',{'condition': {'operator': 'truthy', 'path': '$.inputs.group'}}),
            ('route','batch',{'condition': 'default'}), ('form','child_end',{}), ('batch','end',{})]]
    doc['definition']['output_schema'] = {'type': 'object', 'additionalProperties': True}
    return doc


def advance(env):
    env.db.expire_all()
    env.db.query(ExecutionNodeRun).filter_by(node_type='flow.batch', status='ready').update({'ready_at': utcnow()-timedelta(seconds=1)})
    env.db.commit()
    drain(env)
    env.db.expire_all()


def answer(env, child):
    task = env.db.query(ExecutionHumanTask).filter_by(run_id=child.id, status='open').one()
    submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision, data={'value': child.batch_context['key']})
    env.db.commit()
    advance(env)
    advance(env)


def children(env, parent):
    return env.db.query(ExecutionRun).filter_by(parent_run_id=parent.id).order_by(ExecutionRun.created_at).all()


def test_groups_pin_version_and_only_retry_failed_group(environment):
    env = environment
    workflow = publish_native_document(env.db, env.admin, document())
    data = run(env, workflow.id)
    drain(env); env.db.expire_all()
    parent = env.db.get(ExecutionRun, data['id'])
    first = children(env, parent)[0]
    assert first.status == 'waiting_human', [(n.node_id,n.status,n.error_message) for n in first.node_runs]
    assert [v['status'] for v in child_summaries(env.db, parent)] == ['waiting_human', 'pending', 'pending']
    # Publish an updated graph while group a waits; every later group stays pinned.
    changed = document(); changed['release']['release_version'] = 2; changed['release']['description'] = 'new release'
    next(n for n in changed['definition']['nodes'] if n['id'] == 'child_end')['input_mapping']['new'] = True
    publish_native_document(env.db, env.admin, changed)
    answer(env, first)
    first, second = children(env, parent)
    assert first.status == 'completed' and second.status == 'waiting_human'
    assert second.workflow_version_id == first.workflow_version_id == parent.workflow_version_id
    # Fail the second group's end node once, before it could produce a result.
    task = env.db.query(ExecutionHumanTask).filter_by(run_id=second.id, status='open').one()
    submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision, data={'value': 'b'})
    env.db.commit()
    claimed = claim_next_node(env.db, worker_id=env.worker.worker_id)
    assert claimed.run_id == second.id and claimed.node_id == 'child_end'
    fail_node(env.db, node_run_id=claimed.id, lease_token=claimed.lease_token, error_code='temporary', error_message='retryable fixture')
    env.db.commit(); advance(env)
    assert parent.status == 'failed' and len(children(env, parent)) == 2
    retry_failed_node(env.db, run_id=parent.id, node_id='batch', actor=env.admin)
    env.db.commit(); advance(env); advance(env)
    third = children(env, parent)[2]
    answer(env, third); advance(env)
    assert parent.status == 'completed', parent.error_message
    assert [v['run_id'] for v in parent.output_data['groups']] == [c.id for c in children(env,parent)]
    assert all(c.workflow_version_id == parent.workflow_version_id for c in children(env,parent))
    assert 'new' not in third.output_data
    assert env.db.query(ExecutionHumanTask).count() == 3


def test_group_pause_resume_and_cancel_stop_later_groups(environment):
    env = environment
    workflow = publish_native_document(env.db, env.admin, document())
    data = run(env, workflow.id); drain(env); env.db.expire_all()
    parent = env.db.get(ExecutionRun, data['id']); child = children(env,parent)[0]
    set_run_control_status(env.db, run_id=parent.id, action='pause', actor=env.admin); env.db.commit()
    assert parent.status == child.status == 'paused'
    set_run_control_status(env.db, run_id=parent.id, action='resume', actor=env.admin); env.db.commit()
    assert child.status == 'waiting_human'
    set_run_control_status(env.db, run_id=parent.id, action='cancel', actor=env.admin); env.db.commit(); advance(env)
    assert parent.status == child.status == 'cancelled'
    assert len(children(env,parent)) == 1
    assert all(g['status'] == 'cancelled' for g in child_summaries(env.db,parent))


def test_group_selection_preserves_order_and_rejects_cross_group_reuse():
    items = [{'id': k, 'kind': 'option', 'label': k} for k in 'abcd']
    inputs = {'items': items, 'groups': [{'id': k, 'label': k, 'allowed_selected_counts': [1,2], 'metadata': {}} for k in ['front','back']]}
    data = {'groups': [{'id': 'front', 'selected_ids': ['b','a']}, {'id':'back','selected_ids':['d']}]}
    out = normalize_groups(inputs, data, {}, lambda item: None)
    assert [i['id'] for i in out['groups'][0]['selected_items']] == ['b','a']
    data['groups'][1]['selected_ids'] = ['a']
    with pytest.raises(ExecutionApiError, match='同一候选'): normalize_groups(inputs,data,{},lambda item:None)
    data['groups'][1]['selected_ids'] = []
    with pytest.raises(ExecutionApiError): normalize_groups(inputs,data,{},lambda item:None)
    assert normalize_groups(inputs,data,{'require_all_groups':False},lambda item:None)['unselected_group_ids'] == ['back']


def test_parent_cancel_waits_for_child_settlement(environment):
    env = environment
    workflow=publish_native_document(env.db,env.admin,document())
    data=run(env,workflow.id);drain(env);env.db.expire_all()
    parent=env.db.get(ExecutionRun,data['id']);child=children(env,parent)[0]
    # A callback may still be settling an external result. Parent cancellation
    # must keep that visible until the existing child mechanism finishes it.
    child.status='failure_pending';env.db.commit()
    set_run_control_status(env.db,run_id=parent.id,action='cancel',actor=env.admin);env.db.commit()
    assert parent.status=='cancel_pending' and parent.finished_at is None
    child.status='cancelled';child.finished_at=utcnow();env.db.commit()
    advance(env)
    assert parent.status=='cancelled' and len(children(env,parent))==1
