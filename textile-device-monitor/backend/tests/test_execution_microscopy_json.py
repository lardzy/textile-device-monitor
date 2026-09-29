"""Portable microscopy rules and native image selection, without retired nodes."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution.engine import claim_human_task, submit_human_task
from app.execution.errors import ExecutionApiError
from app.execution.models import ExecutionRun, ExecutionNodeRun, ExecutionHumanTask, ExecutionTaskSnapshotCache
from app.execution.v2.data_handlers import python_test
from app.execution.v2.designer import compile_document
from app.execution.v2.native_handlers import _file_query
from native_io_helpers import NUMBER, image_files, task_snapshot
from workflow_native_helpers import environment, stage, publish, run, drain

PATH = Path(__file__).parents[1]/'app/execution/v2/resources/workflows/fiber-microscopy-v2.json'


def document():
    return json.loads(PATH.read_text())


def trial(name, **inputs):
    item = next(n for n in document()['definition']['nodes'] if n['id'] == name)
    if name == 'prepare_form':
        profile = next(n for n in document()['definition']['nodes'] if n['id'] == 'profiles')['input_mapping']['profiles'][0]
        inputs.setdefault('rules', profile['form_rules'])
    return python_test(item['config'], {**item['input_mapping'], **inputs})


def test_portable_rules_multiple_projects_fullwidth_and_missing_candidates():
    p = {'project_key': 'project1', 'check_item_no': '5103.5', 'check_item_name': '纤维微观形貌', 'check_method': 'GB/T 36422-2018'}
    item = {'id': 'a', 'kind': 'artifact', 'label': 'a.png', 'relative_path': '260191178粘/a.png', 'fingerprint': '1:2'}
    projects = trial('profiles', snapshot={'projects': [p, {**p, 'project_key': 'project2'}]})['output']['projects']
    args = dict(inspection_number='２６０１９１１７８', projects=projects, items=[item], truncated=False)
    report = trial('candidates', **args)
    assert report['passed'], report
    assert len(report['output']['projects']) == 2
    assert report['output']['items'][0]['kind'] == 'image'
    assert 4 not in trial('selection', project=projects[0])['output']['allowed_selected_counts']
    assert not trial('candidates', **{**args, 'truncated': True})['passed']
    assert not trial('candidates', **{**args, 'items': []})['passed']
    assert not trial('candidates', **{**args, 'projects': []})['passed']


def test_profiles_match_exact_pairs_and_never_equate_numeric_codes():
    profiles = next(n for n in document()['definition']['nodes'] if n['id'] == 'profiles')['input_mapping']['profiles']
    profiles[1]['enabled'] = True
    rows = [{'project_key': str(i), 'check_item_no': code, 'check_item_name': '微观形貌', 'check_method': method}
            for i, (code, method) in enumerate([('5103.5', 'GB/T 36422-2018'), ('5103.05', '按客户要求'),
              ('5103.5', '按客户要求'), ('5103.05', 'GB/T 36422-2018'), ('5103.050', '按客户要求')])]
    report = trial('profiles', profiles=profiles, snapshot={'projects': rows})
    assert report['passed'], report
    assert [p['metadata']['project']['project_key'] for p in report['output']['projects']] == ['0', '1']
    profiles[1]['enabled'] = False
    assert len(trial('profiles', profiles=profiles, snapshot={'projects': rows})['output']['projects']) == 1
    profiles.append(deepcopy(profiles[0]))
    assert not trial('profiles', profiles=profiles, snapshot={'projects': rows})['passed']


def test_ambiguous_profiles_remain_explicit_choices_and_counts_derive_from_templates():
    profiles = next(n for n in document()['definition']['nodes'] if n['id'] == 'profiles')['input_mapping']['profiles'][:1]
    profiles.append({**deepcopy(profiles[0]), 'id': 'alternative', 'name': '另一个方案'})
    report = trial('profiles', profiles=profiles, snapshot={'projects': [{'project_key': 'p', 'check_item_no': '5103.5', 'check_item_name': '膜平面形貌', 'check_method': 'GB/T 36422-2018'}]})
    assert report['passed'], report
    choices = report['output']['projects']
    assert len({p['id'] for p in choices}) == 2
    choices[0]['metadata']['profile']['templates'] = {'2': choices[0]['metadata']['profile']['templates']['2']}
    assert trial('selection', project=choices[0])['output']['allowed_selected_counts'] == [2]


def test_form_defaults_and_judgement_are_json_code():
    base = dict(snapshot={'sample_names': ['完整-样品名称'], 'check_basis': '依据一'}, images=[{}],
                project={'label': '项目', 'metadata': {'project': {'sample_identify': None, 'give_judgement': 0, 'check_count': 4}}})
    result = trial('prepare_form', **base)
    assert result['passed'], result
    assert result['output']['defaults'] == {'sample_name': '完整-样品名称', 'sample_identity': '', 'remark': ''}
    assert 'judgement' not in result['output']['form_schema']['properties']
    base['project']['metadata']['project'].update(sample_identify='正面、背面', give_judgement=1)
    result = trial('prepare_form', **base)
    assert result['passed'], result
    assert result['output']['form_schema']['properties']['sample_identity']['enum'] == ['正面', '背面']
    assert result['output']['defaults']['judge_basis'] == '依据一'


def test_image_query_scope_and_generic_preview(environment):
    env = environment
    image_files(env, 4)
    node = {'type_version': 2, 'config': {'root_id': 'electron_microscopy_records', 'limit': 1000, 'sort': 'name_asc'}}
    context = SimpleNamespace(db=env.db, node=node, input_data={'query': NUMBER, 'relative_directory': NUMBER}, run=SimpleNamespace(inspection_number=NUMBER))
    result = _file_query(context)
    assert result['count'] == 4 and not result['truncated']
    context.input_data['relative_directory'] = NUMBER + 'suffix'
    assert _file_query(context)['count'] == 0
    context.input_data['relative_directory'] = '../escape'
    with pytest.raises(ExecutionApiError): _file_query(context)
    entry = result['items'][0]
    assert env.client.get('/api/execution/v1/files/index/'+entry['id']+'/preview').status_code == 200


def test_native_selection_rejects_unsupported_counts_without_completing_task(environment):
    env = environment
    image_files(env, 4)
    task_snapshot(env, 'microscopy')
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    snap = deepcopy(cache.snapshot)
    snap['projects'][0].update(check_item_no='5103.5', seq_num=1)
    cache.snapshot = snap
    env.db.commit()
    doc = document()
    last = next(i for i, n in enumerate(doc['definition']['nodes']) if n['id'] == 'form')
    doc['definition']['nodes'] = doc['definition']['nodes'][:last+1] + [doc['definition']['nodes'][-1]]
    doc['definition']['nodes'][-1]['input_mapping'] = {'form': '$.nodes.form.output'}
    doc['definition']['output_schema'] = {'type': 'object', 'properties': {'form': {'type': 'object'}}, 'required': ['form'], 'additionalProperties': False}
    doc['definition']['edges'] = [{'id':a['id']+'-'+b['id'],'source':a['id'],'target':b['id'],'join_policy':'all'} for a,b in zip(doc['definition']['nodes'],doc['definition']['nodes'][1:])]
    doc['resources']['root_slots'] = [r for r in doc['resources']['root_slots'] if r['slot_id']=='electron_microscopy_records']
    doc['resources']['credential_slots'] = []
    next(n for n in doc['definition']['nodes'] if n['id'] == 'task')['input_mapping']['refresh'] = False
    compiled = compile_document(doc)
    assert compiled['content_valid'], compiled['issues']
    release = publish(env, stage(env, compiled['document']))
    record = run(env, release['workflow_id'], inputs={'inspection_number': NUMBER, 'relative_directory': ''})
    drain(env)
    env.db.expire_all()
    task = env.db.query(ExecutionHumanTask).filter_by(run_id=record['id'], status='open').one()
    assert env.db.get(ExecutionNodeRun, task.node_run_id).node_id == 'images'
    images = env.db.query(ExecutionNodeRun).filter_by(run_id=record['id'], node_id='images').one().input_data['items']
    claim_human_task(env.db, task_id=task.id, expected_revision=task.revision, actor=env.admin)
    env.db.commit()
    with pytest.raises(ExecutionApiError):
        submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision,
                            data={'selected_ids': [i['id'] for i in images]})
    env.db.rollback()
    task = env.db.get(ExecutionHumanTask, task.id)
    order = [images[i]['id'] for i in (2, 0, 1)]
    submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision, data={'selected_ids': order})
    env.db.commit()
    drain(env)
    env.db.expire_all()
    current = env.db.get(ExecutionRun, record['id'])
    nodes = env.db.query(ExecutionNodeRun).filter_by(run_id=current.id).all()
    assert current.status == 'completed', [(n.node_id, n.status, n.error_message) for n in nodes]
    assert next(n for n in nodes if n.node_id == 'images').output_data['selected_ids'] == order
