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
from app.execution.v2.text_handlers import segment
from app.execution.v2.designer import compile_document
from app.execution.v2.native_handlers import _file_query
from native_io_helpers import NUMBER, image_files, task_snapshot
from workflow_native_helpers import environment, stage, publish, run, drain

PATH = Path(__file__).parents[1]/'app/execution/v2/resources/workflows/fiber-microscopy-v2.json'


def document():
    return json.loads(PATH.read_text())


def trial(name, **inputs):
    item = next(n for n in document()['definition']['nodes'] if n['id'] == name)
    if name == 'selection':
        inputs.setdefault('snapshot', {})
        snapshot = inputs['snapshot']
        inputs.setdefault('name_tokens', segment(SimpleNamespace(node={'config': {}}, input_data={
            'texts': snapshot.get('sample_names'), 'text': snapshot.get('sample_name')}))['tokens'])
    if name == 'prepare_form':
        profile = next(n for n in document()['definition']['nodes'] if n['id'] == 'profiles')['input_mapping']['profiles'][0]
        inputs.setdefault('rules', profile['form_rules'])
        inputs.setdefault('sample_identity', '')
        inputs.setdefault('sample_name', '人工名称')
    if name == 'payload':
        profile = next(n for n in document()['definition']['nodes'] if n['id'] == 'profiles')['input_mapping']['profiles'][0]
        inputs.setdefault('rules', profile)
        inputs.setdefault('templates', profile['templates'])
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
    assert result['output']['defaults'] == {'sample_name': '人工名称', 'sample_identity': '', 'remark': ''}
    assert 'judgement' not in result['output']['form_schema']['properties']
    base['project']['metadata']['project'].update(sample_identify='正面、背面', give_judgement=1)
    result = trial('prepare_form', **base, sample_identity='正面')
    assert result['passed'], result
    assert result['output']['defaults']['sample_identity'] == '正面'
    assert result['output']['form_schema']['properties']['sample_identity']['const'] == '正面'
    assert result['output']['form_schema']['properties']['sample_identity']['enum'] == ['正面', '背面']
    assert result['output']['defaults']['judge_basis'] == '依据一'


@pytest.mark.parametrize('raw, expected', [
    (['重装徒步冲锋衣 ７号，薄膜（测试用）', '薄膜'], ['重装徒步冲锋衣', '薄膜', '测试用']),
    (['唯一名称'], ['唯一名称']),
    ([], []),
    (['很长的名称' * 150], []),
])
def test_shared_name_candidates_always_require_human_input(raw, expected):
    choice = trial('profiles', snapshot={'projects': [{'project_key': 'p', 'check_item_no': '5103.05',
        'check_item_name': '微观形貌', 'check_method': '按客户要求', 'sample_identify': '正面、反面、横截面'}]})['output']['projects'][0]
    result = trial('selection', project=choice, snapshot={'sample_names': raw})
    assert result['passed'], result
    selection = result['output']
    field = selection['form_schema']['properties']['sample_name']
    assert field['default'] == ''
    assert set(expected) <= set(field['x-suggestions'])
    assert len(field['x-suggestions']) == len(set(field['x-suggestions']))
    assert field['x-suggestion-display'] == 'buttons'
    if raw and raw[0].startswith('重装'):
        assert '冲锋衣' in field['x-suggestions']
    assert selection['context']['任务单样品名称'] == ('\n'.join(raw) or '任务单未提供，请人工填写')
    groups = [{**g, 'selected_ids': ['image'], 'selected_items': []} for g in selection['groups']]
    planned = trial('batch_plan', groups=groups, form_data={'sample_name': '  人工简名  '})
    assert planned['passed'], planned
    assert [item['inputs']['execution_group']['sample_name'] for item in planned['output']['items']] == ['人工简名'] * 3


def test_confirmed_shared_name_overrides_snapshot_and_profile_defaults():
    base = dict(snapshot={'sample_names': ['任务单冗长名称']}, images=[{}], sample_name='人工简名',
        project={'label': '项目', 'metadata': {'project': {'sample_identify': None, 'give_judgement': 0}}},
        rules={'name_separator': ';', 'identity_separator': ';', 'defaults': {'sample_name': '规则名称'}})
    result = trial('prepare_form', **base)
    assert result['passed'], result
    assert result['output']['defaults']['sample_name'] == '人工简名'
    assert result['output']['form_schema']['properties']['sample_name']['const'] == '人工简名'
    assert not trial('prepare_form', **{**base, 'sample_name': '  '})['passed']


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
    by_id = {n['id']: n for n in doc['definition']['nodes']}
    doc['definition']['nodes'] = [by_id[k] for k in ('start', 'task', 'profiles', 'files', 'candidates', 'project', 'name_tokens', 'selection', 'images', 'end')]
    by_id['end']['input_mapping'] = {'groups': '$.nodes.images.output.groups'}
    doc['definition']['output_schema'] = {'type': 'object', 'additionalProperties': True}
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
                            data={'groups': [{'id': '1', 'selected_ids': [i['id'] for i in images]}]})
    env.db.rollback()
    task = env.db.get(ExecutionHumanTask, task.id)
    order = [images[i]['id'] for i in (2, 0, 1)]
    for form_data in ({}, {'sample_name': '  '}, {'sample_name': 'x' * 501}):
        with pytest.raises(ExecutionApiError, match='人工任务输入校验失败'):
            submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision,
                data={'groups': [{'id': '1', 'selected_ids': order}], 'form_data': form_data})
        env.db.rollback()
        task = env.db.get(ExecutionHumanTask, task.id)
    submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision, data={'groups': [{'id': '1', 'selected_ids': order}], 'form_data': {'sample_name': '人工名称'}})
    env.db.commit()
    drain(env)
    env.db.expire_all()
    current = env.db.get(ExecutionRun, record['id'])
    nodes = env.db.query(ExecutionNodeRun).filter_by(run_id=current.id).all()
    assert current.status == 'completed', [(n.node_id, n.status, n.error_message) for n in nodes]
    assert next(n for n in nodes if n.node_id == 'images').output_data['groups'][0]['selected_ids'] == order
