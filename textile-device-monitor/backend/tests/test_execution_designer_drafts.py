from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.execution.v2.designer import starter_document, compile_document
from app.execution.v2.data_handlers import python_test, python_compute
from app.execution.models import ExecutionRun
from workflow_native_helpers import environment, request, stage, publish, run, drain


def test_empty_draft_conflict_and_directory(environment):
    env = environment
    document = starter_document()
    document['definition'].update(nodes=[], edges=[])
    draft = request(env, 'POST', 'v2/designer/drafts', {'document': document, 'bindings': {}}, status=201)
    assert draft['published_version'] is None
    listing = env.user_client.get('/api/execution/v1/workflows').json()
    assert not listing['items'] if isinstance(listing, dict) else not listing
    path = f"v2/workflows/{draft['workflow_id']}/designer-draft"
    edited = deepcopy(document)
    edited['release']['name'] = '仍可保存的空图'
    saved = request(env, 'PUT', path, {'revision': draft['revision'], 'document': edited, 'bindings': {}})
    assert saved['revision'] == draft['revision'] + 1
    conflict = request(env, 'PUT', path, {'revision': draft['revision'], 'document': document, 'bindings': {}}, status=409)
    assert conflict['code'] == 'draft_revision_changed'
    assert request(env, 'GET', path)['document']['release']['name'] == edited['release']['name']
    assert not compile_document(edited)['content_valid']
    second = request(env, 'POST', 'v2/designer/drafts', {'document': document}, status=201)
    assert second['document']['release']['slug'] != draft['document']['release']['slug']


def test_publish_preserves_newer_designer_draft(environment):
    env = environment
    document = starter_document()
    draft = request(env, 'POST', 'v2/designer/drafts', {'document': document}, status=201)
    release = stage(env, compile_document(document)['document'])
    document['definition'].update(nodes=[], edges=[])
    saved = request(env, 'PUT', f"v2/workflows/{draft['workflow_id']}/designer-draft", {'revision': draft['revision'], 'document': document, 'bindings': {}})
    deployed = publish(env, release)
    current = request(env, 'GET', f"v2/workflows/{draft['workflow_id']}/designer-draft")
    assert current['document']['definition']['nodes'] == []
    assert current['revision'] > saved['revision']
    assert current['published_version'] == 1
    record = run(env, draft['workflow_id'], inputs={'inspection_number': '26W006824'})
    drain(env)
    env.db.expire_all()
    assert env.db.get(ExecutionRun, record['id']).status == 'completed'


def test_edge_conditions_check_missing_and_future_sources():
    document = starter_document()
    document['definition']['nodes'].insert(1, {'id': 'branch', 'type': 'flow.branch', 'type_version': 1, 'name': '分支', 'config': {'expression_version': 1, 'multi_match': 'all'}, 'input_mapping': {}})
    document['definition']['edges'] = [{'id': 'test', 'source': 'branch', 'target': 'end', 'join_policy': 'all'}, {'id': 'before', 'source': 'start', 'target': 'branch', 'join_policy': 'all'}]
    document['definition']['edges'].append({'id': 'default', 'source': 'branch', 'target': 'end', 'condition': 'default', 'join_policy': 'all'})
    document['definition']['edges'][0]['condition'] = {'path': '$.nodes.deleted.output.ready', 'operator': 'truthy'}
    missing = compile_document(document)
    assert not missing['content_valid'], missing
    document['definition']['edges'][0]['condition']['path'] = '$.nodes.end.output.ready'
    assert not compile_document(document)['content_valid']
    document['definition']['edges'][0]['condition']['path'] = '$.nodes.start.output.inspection_number'
    report = compile_document(document)
    assert report['content_valid'], report['issues']


SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}
CONFIG = {'runtime_version': 1, 'timeout_seconds': 1, 'input_schema': SCHEMA, 'output_schema': SCHEMA,
          'code': "import unicodedata\ndef main(inputs):\n    print('trial')\n    return {'value': unicodedata.normalize('NFKC', inputs['value'])}"}


@pytest.mark.parametrize('value,expected', [('１００','100'), ('１０００','1000'), ('１００．５','100.5'), ('','')])
def test_trial_and_worker_compute_agree(value, expected):
    inputs = {'value': value}
    report = python_test(CONFIG, inputs)
    assert report['passed'] and report['output'] == {'value': expected}
    assert report['logs'] == 'trial\n' and report['duration_ms'] > 0
    assert python_compute(SimpleNamespace(node={'config': CONFIG}, input_data=inputs)) == report['output']


def test_python_diagnostics_and_api(environment):
    config = {**CONFIG, 'code': 'def main(inputs):\n    print("before")\n    return 1 / 0'}
    result = request(environment, 'POST', 'v2/designer/python-test', {'config': config, 'inputs': {'value': ''}})
    assert not result['passed'] and result['error']['line'] == 3 and result['logs'] == 'before\n'
    assert python_test(CONFIG, {'value': 1})['error']['stage'] == 'input'
    assert python_test({**CONFIG, 'code': 'def main(inputs):\n    return {"value": 1}'}, {'value': ''})['error']['stage'] == 'output'
    assert python_test({**CONFIG, 'code': 'def main(inputs):\n    while True: pass'}, {'value': ''})['error']['stage'] == 'timeout'
    assert python_test({**CONFIG, 'code': 'def main(:\n    pass'}, {'value': ''})['error']['line'] == 1


def test_sample_assertion_deleted_source_blocks_publish():
    document = starter_document()
    document['fixtures'] = [{'fixture_id': 'example', 'name': 'example', 'inputs': {'inspection_number': 'one'}, 'globals': {}, 'mocks': [], 'assertions': [{'path': '$.nodes.deleted.output.result', 'operator': 'exists'}]}]
    report = compile_document(document)
    assert not report['content_valid']
    assert any(issue['code'] == 'mapping_source_node_missing' for issue in report['issues']), report['issues']
