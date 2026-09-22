"""Native graph lifecycle: leases, human input, branches and immutable Runs."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from app.execution.engine import (claim_next_node, complete_node, create_run, execute_claimed_node,
    fail_node, reject_human_task, retry_failed_node, set_run_control_status, submit_human_task)
from app.execution.errors import ExecutionApiError
from app.execution.models import (ExecutionEvent, ExecutionHumanTask, ExecutionNodeAttempt,
    ExecutionNodeRun, ExecutionRun, ExecutionWorkflowVersion, utcnow)
from app.execution.v2.designer import starter_document
from workflow_native_helpers import environment, publish_native_document, drain


@pytest.fixture
def graph(environment):
    env = environment
    document = starter_document()
    document['release'].update(slug='lifecycle', name='Native lifecycle')
    answer = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer'], 'additionalProperties': False}
    document['definition']['output_schema'] = answer
    document['definition']['nodes'].insert(1, {'id': 'input', 'type': 'human.form', 'type_version': 1,
        'name': '填写', 'config': {'title': '填写结果', 'form_schema': answer}, 'input_mapping': {}})
    document['definition']['nodes'][-1]['input_mapping'] = {'answer': '$.nodes.input.output.answer'}
    document['definition']['edges'] = [
        {'id': 'a', 'source': 'start', 'target': 'input', 'join_policy': 'all'},
        {'id': 'b', 'source': 'input', 'target': 'end', 'join_policy': 'all'}]
    env.workflow = publish_native_document(env.db, env.admin, document)
    env.document = document
    return env


def start(env, key=None):
    run, _ = create_run(env.db, workflow=env.workflow, actor=env.admin,
        inspection_number='TEST-001', input_data={}, global_data={}, idempotency_key=key or str(uuid4()))
    env.db.commit()
    return run


def claim(env):
    node = claim_next_node(env.db, worker_id=env.worker.worker_id, lease_seconds=1)
    assert node is not None
    env.db.commit()
    return node


def control(env, run, action):
    set_run_control_status(env.db, run_id=run.id, action=action, actor=env.admin)
    env.db.commit()
    env.db.refresh(run)


def human(env, run):
    drain(env)
    return env.db.query(ExecutionHumanTask).filter_by(run_id=run.id).one()


def submit(env, task, answer='结果'):
    submit_human_task(env.db, task_id=task.id, expected_revision=task.revision,
        data={'answer': answer}, actor=env.admin)
    env.db.commit()


def test_human_wait_survives_session_and_output_completes(graph):
    run = start(graph)
    task = human(graph, run)
    assert task.status == 'open'
    with graph.Session() as other:
        assert other.get(ExecutionRun, run.id).status == 'waiting_human'
        assert other.get(ExecutionHumanTask, task.id).status == 'open'
    submit(graph, task)
    drain(graph)
    graph.db.refresh(run)
    assert run.status == 'completed' and run.output_data == {'answer': '结果'}


def test_same_idempotency_key_creates_one_run_and_changed_input_conflicts(graph):
    first = start(graph, 'same-key')
    assert start(graph, 'same-key').id == first.id
    with pytest.raises(ExecutionApiError, match='幂等'):
        create_run(graph.db, workflow=graph.workflow, actor=graph.admin, inspection_number='CHANGED',
            input_data={}, global_data={}, idempotency_key='same-key')
    assert graph.db.query(ExecutionRun).count() == 1


def test_published_snapshot_ignores_later_draft_and_capability_edits(graph):
    run = start(graph)
    before = deepcopy(run.definition_snapshot)
    version = graph.db.query(ExecutionWorkflowVersion).one()
    graph.workflow.designer_draft = {'document': {'definition': {'nodes': []}}}
    graph.workflow.draft_definition = {}
    graph.workflow.capabilities = {'hidden': True}
    graph.db.commit()
    graph.db.refresh(run)
    assert run.definition_snapshot == before == version.definition
    assert run.capabilities_snapshot == version.capabilities


def test_invalid_human_value_preserves_pending_task(graph):
    run = start(graph); task = human(graph, run)
    with pytest.raises(ExecutionApiError) as error:
        submit(graph, task, 123)
    assert error.value.code == 'human_task_input_invalid'
    graph.db.rollback()
    assert task.status == 'open'


def test_expired_lease_is_reclaimed_and_old_completion_rejected(graph):
    run = start(graph); first = claim(graph); token = first.lease_token
    first.lease_expires_at = utcnow() - timedelta(seconds=2)
    graph.db.commit()
    second = claim(graph)
    assert second.id == first.id and second.lease_token != token
    with pytest.raises(ExecutionApiError):
        complete_node(graph.db, node_run_id=first.id, lease_token=token, output_data={})
    graph.db.rollback()
    assert graph.db.query(ExecutionNodeAttempt).filter_by(node_run_id=first.id).count() == 2


@pytest.mark.parametrize('failure', [False, True])
def test_pause_keeps_node_completion_or_failure_until_resume(graph, failure):
    run = start(graph); node = claim(graph); token = node.lease_token
    control(graph, run, 'pause')
    if failure:
        fail_node(graph.db, node_run_id=node.id, lease_token=token, error_code='test_failure', error_message='测试失败')
    else:
        complete_node(graph.db, node_run_id=node.id, lease_token=token, output_data={})
    graph.db.commit(); graph.db.refresh(run)
    assert run.status == 'paused'
    control(graph, run, 'resume')
    assert run.status == ('failed' if failure else 'running')


def test_cancel_running_node_rejects_late_completion(graph):
    run = start(graph); node = claim(graph); token = node.lease_token
    control(graph, run, 'cancel')
    assert run.status == 'cancelled'
    with pytest.raises(ExecutionApiError):
        complete_node(graph.db, node_run_id=node.id, lease_token=token, output_data={})


def test_cancel_closes_human_and_rejects_submission(graph):
    run = start(graph); task = human(graph, run)
    control(graph, run, 'cancel'); graph.db.refresh(task)
    assert task.status == 'cancelled'
    with pytest.raises(ExecutionApiError): submit(graph, task)


def test_paused_human_submission_does_not_resume_run(graph):
    run = start(graph); task = human(graph, run)
    control(graph, run, 'pause'); submit(graph, task)
    graph.db.refresh(run); assert run.status == 'paused'
    assert claim_next_node(graph.db, worker_id=graph.worker.worker_id) is None
    control(graph, run, 'resume'); drain(graph); graph.db.refresh(run)
    assert run.status == 'completed'


def test_failed_node_retry_keeps_run_identity(graph):
    run = start(graph); node = claim(graph)
    fail_node(graph.db, node_run_id=node.id, lease_token=node.lease_token, error_code='test_failure', error_message='可重试')
    graph.db.commit(); graph.db.refresh(run); assert run.status == 'failed'
    retry_failed_node(graph.db, run_id=run.id, node_id='start', actor=graph.admin)
    graph.db.commit(); task = human(graph, run); submit(graph, task); drain(graph)
    graph.db.refresh(run); assert run.status == 'completed'
    assert graph.db.query(ExecutionRun).count() == 1


@pytest.mark.parametrize('terminal', ['completed', 'cancelled'])
def test_retry_is_rejected_for_terminal_run(graph, terminal):
    run = start(graph)
    if terminal == 'completed': submit(graph, human(graph, run)); drain(graph)
    else: control(graph, run, 'cancel')
    with pytest.raises(ExecutionApiError) as error:
        retry_failed_node(graph.db, run_id=run.id, node_id='start', actor=graph.admin)
    assert error.value.code == 'run_not_retryable'


@pytest.mark.parametrize('join_policy', ['all', 'any'])
def test_join_wakes_once_at_declared_threshold(environment, join_policy):
    env = environment; doc = starter_document()
    nodes = [('fork', 'flow.fork', {}), ('left', 'flow.fork', {}), ('right', 'flow.fork', {}),
        ('join', 'flow.join', {'mode': 'all_selected' if join_policy == 'all' else 'first_selected'})]
    doc['definition']['nodes'][1:1] = [{'id': ident, 'type': kind, 'type_version': 1, 'name': ident,
        'config': config, 'input_mapping': {}} for ident, kind, config in nodes]
    doc['definition']['edges'] = [{'id': str(i), 'source': a, 'target': b, 'join_policy': join_policy if b == 'join' else 'all'}
        for i, (a,b) in enumerate([('start','fork'),('fork','left'),('fork','right'),('left','join'),('right','join'),('join','end')])]
    env.workflow = publish_native_document(env.db, env.admin, doc); run = start(env)
    for _ in range(2):
        node=claim(env); execute_claimed_node(env.db, node.id, node.lease_token); env.db.commit()
    branches=[claim(env),claim(env)]
    complete_node(env.db,node_run_id=branches[0].id,lease_token=branches[0].lease_token,output_data={});env.db.commit()
    join=env.db.query(ExecutionNodeRun).filter_by(run_id=run.id,node_id='join').one()
    assert join.status == ('ready' if join_policy == 'any' else 'pending')
    complete_node(env.db,node_run_id=branches[1].id,lease_token=branches[1].lease_token,output_data={});env.db.commit()
    drain(env); env.db.refresh(run)
    assert run.status == 'completed'
    assert env.db.query(ExecutionNodeAttempt).filter_by(node_run_id=join.id).count()==1
