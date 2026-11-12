"""Cross-section delivery uses portable rules, real files, and simulated receipts."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
import xlrd

from app.config import settings
from app.execution.engine import submit_human_task
from app.execution.external_operations import (
    _operation_stage_profile, claim_approved_external_operation,
    complete_external_attempt, record_external_attempt_stage,
)
from app.execution.models import (
    ExecutionExternalOperation, ExecutionHumanTask, ExecutionNodeRun,
    ExecutionProjectRule, ExecutionRun, ExecutionStorageRoot, ExecutionTaskSnapshotCache,
)
from app.execution.v2.canonical import canonical_sha256
from app.execution.v2.data_handlers import python_test
from app.execution.v2.workbook_render import render_file
from bridge_test_helpers import upload_receipt, review_receipt
from native_io_helpers import NUMBER, image_files, task_snapshot
from test_execution_microscopy_full import excel_receipt
from test_execution_run_groups import advance
from tests.test_execution_connector_operations import operation_env
from workflow_native_helpers import environment, publish_native_document, run, drain

ROOT = Path(__file__).parents[2]
DOCUMENTS = [ROOT / 'backend/app/execution/v2/resources/workflows/fiber-microscopy-v2.json',
             ROOT / 'docs/execution-v2/examples/fiber-microscopy-v2.json']


def call(doc, node_id, **inputs):
    node = next(item for item in doc['definition']['nodes'] if item['id'] == node_id)
    result = python_test(node['config'], {**node['input_mapping'], **inputs})
    assert result['passed'], result
    return result['output']


def project(**changes):
    return {'project_key': 'cross-section-project', 'task_check_item_id': 'task',
            'check_item_id': 'item', 'check_item_no': '5103.426', 'check_item_name': '纤维横截面',
            'check_method': 'GB/T 36422-2018', 'seq_num': 1, 'check_count': 1,
            'register_count': 0, 'give_judgement': 0, 'sample_identify': '横截面', **changes}


def prepared(doc, count):
    choice = call(doc, 'profiles', snapshot={'projects': [project()]})['projects'][0]
    rules = choice['metadata']['profile']
    images = [{'id': str(i), 'root_id': 'images', 'relative_path': f'{NUMBER}/{i}.bmp',
               'fingerprint': '1:2'} for i in range(count)]
    return call(doc, 'payload', inspection_number=NUMBER, project=choice,
                form={'sample_name': '人工简名', 'sample_identity': '横截面', 'remark': '备注'},
                images=images, rules=rules, templates=rules['templates'])


@pytest.mark.parametrize('path', DOCUMENTS, ids=['bundled', 'exported'])
def test_cross_section_match_is_exact_and_image_counts_are_bound(path):
    doc = json.loads(path.read_text())
    rows = [project(), project(project_key='fullwidth', check_item_no='５１０３．４２６'),
            project(project_key='wrong-method', check_method='按客户要求'),
            project(project_key='different-code', check_item_no='5103.4260')]
    choices = call(doc, 'profiles', snapshot={'projects': rows})['projects']
    assert [item['metadata']['project']['project_key'] for item in choices] == ['cross-section-project', 'fullwidth']
    choice = choices[0]
    selection = call(doc, 'selection', project=choice, snapshot={}, name_tokens=[])
    assert selection['allowed_selected_counts'] == [1, 2, 3]
    assert selection['groups'][0]['allowed_selected_counts'] == [1, 2, 3]
    assert selection['form_schema']['required'] == ['sample_name']
    assert selection['form_schema']['properties']['sample_name']['default'] == ''
    group_node = next(n for n in doc['definition']['nodes'] if n['id'] == 'current_group')
    for count in (0, 4):
        images = [{'id': str(i), 'kind': 'image', 'label': f'{i}.bmp',
                   'fingerprint': '1:2'} for i in range(count)]
        result = python_test(group_node['config'], {
            'group': {'choice_id': choice['id'], 'sample_identity': '横截面',
                      'sample_name': '人工简名', 'images': images},
            'projects': choices, 'items': images})
        assert not result['passed']
        assert '图片数量' in result['error']['message']


@pytest.mark.parametrize('path', DOCUMENTS, ids=['bundled', 'exported'])
@pytest.mark.parametrize('count', [1, 2, 3])
def test_cross_section_registration_templates_paths_and_feed_cells(tmp_path, path, count):
    doc = json.loads(path.read_text())
    data = prepared(doc, count)
    assert data['values']['title'] == '纤维横截面原始记录'
    assert data['values']['method'] == 'GB/T 36422-2018'
    assert data['original_filename'] == f'{NUMBER}-横截面-纤维横截面-原始记录.xls'
    assert data['target_directory'] == f'数据分析中心/3-报告上传图片/8-材料检测中心/1-微观形貌-GB T 36422/{NUMBER}'
    assert [item['target_filename'] for item in data['files']] == [
        f'{NUMBER}-横截面{("-" + str(i)) if i else ""}.bmp' for i in range(count)]
    expected_name = '纤维横截面.xls' if count == 1 else f'纤维横截面-{count}张图.xls'
    assert data['registration_template']['template_name'] == expected_name
    node = next(n for n in doc['definition']['nodes'] if n['id'] == 'check')
    ref = node['config']['templates'][data['template_key']]
    template = ROOT / 'backend/app/execution/templates' / ref['relative_path']
    assert hashlib.sha256(template.read_bytes()).hexdigest() == ref['sha256']
    target = tmp_path / 'cross-section.xls'
    result = render_file(template, target, values=data['values'], fields=node['config']['fields'])
    assert result['cells_verified'] == 17
    book = xlrd.open_workbook(target)
    try:
        sheet = book.sheet_by_name('Sheet1')
        assert sheet.cell_value(3, 44) == NUMBER  # AS4
        assert sheet.cell_value(6, 2) == '纤维横截面'  # C7 visible title
        assert sheet.cell_value(6, 60) == '纤维横截面'  # BI7
        assert sheet.cell_value(6, 62) == '横截面'  # BK7
        assert [sheet.cell_value(row, 60) for row in range(7, 13)] == [
            'GB/T 36422-2018', '', '', '', '备注', '']
    finally:
        book.release_resources()


def test_cross_section_source_export_and_integrity_remain_consistent():
    bundled, exported = [json.loads(path.read_text()) for path in DOCUMENTS]
    assert bundled == exported
    payload = {key: value for key, value in bundled.items() if key != 'integrity'}
    assert canonical_sha256(payload) == bundled['integrity']['digest']
    nodes = {n['id']: n for n in bundled['definition']['nodes']}
    source_profiles = json.loads((ROOT / 'tools/workflow_sources/microscopy_profiles.json').read_text())
    assert nodes['profiles']['input_mapping']['profiles'] == source_profiles
    source_templates = json.loads((ROOT / 'tools/workflow_sources/microscopy_templates.json').read_text())
    for profile in source_profiles:
        for binding in profile['templates'].values():
            source = source_templates[binding['template_key']]
            ref = nodes['check']['config']['templates'][binding['template_key']]
            assert ref['relative_path'] == source['local_asset_name']
            assert ref['sha256'] == source['local_asset_sha256']
    assert all('implementation_digest' not in item for item in bundled['dependencies']['node_types'])


@pytest.mark.parametrize('count', [1, 3])
def test_cross_section_worker_runs_selection_and_prepares_registration(environment, count):
    env = environment
    image_files(env, count)
    task_snapshot(env, 'cross_section')
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    snapshot = deepcopy(cache.snapshot)
    snapshot['projects'][0].update({key: value for key, value in project().items()
                                   if key not in ('project_key', 'task_check_item_id', 'check_item_id')})
    cache.snapshot = snapshot
    env.db.commit()
    doc = json.loads(DOCUMENTS[1].read_text())
    excluded = {'original', 'check', 'place', 'submit_branch', 'upload', 'review', 'entry', 'cancelled'}
    doc['definition']['nodes'] = [n for n in doc['definition']['nodes'] if n['id'] not in excluded]
    doc['definition']['edges'] = [e for e in doc['definition']['edges']
                                  if e['source'] not in excluded and e['target'] not in excluded]
    doc['definition']['edges'].append({'id': 'prepared', 'source': 'payload', 'target': 'group_end', 'join_policy': 'all'})
    nodes = {n['id']: n for n in doc['definition']['nodes']}
    nodes['group_end']['input_mapping'] = {'prepared': '$.nodes.payload.output'}
    nodes['task']['input_mapping']['refresh'] = False
    doc['resources']['root_slots'] = [slot for slot in doc['resources']['root_slots']
                                      if slot['slot_id'] == 'electron_microscopy_records']
    doc['resources']['credential_slots'] = []
    workflow = publish_native_document(env.db, env.admin, doc, {
        'root_slots': {'electron_microscopy_records': {'root_id': 'electron_microscopy_records', 'revision': 1}}})
    parent = run(env, workflow.id, inputs={'inspection_number': NUMBER, 'relative_directory': ''})
    drain(env)
    env.db.expire_all()
    assert env.db.get(ExecutionRun, parent['id']).status == 'waiting_human', [
        (n.node_id, n.status, n.error_message) for n in env.db.query(ExecutionNodeRun).filter_by(run_id=parent['id'])]
    task = env.db.query(ExecutionHumanTask).filter_by(run_id=parent['id'], status='open').one()
    selected = env.db.get(ExecutionNodeRun, task.node_run_id).input_data['items']
    submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision,
        data={'groups': [{'id': '1', 'selected_ids': [item['id'] for item in reversed(selected)]}],
              'form_data': {'sample_name': '人工简名'}})
    env.db.commit()
    advance(env)
    advance(env)  # Let the parent collect the completed child on its next batch poll.
    env.db.expire_all()
    current = env.db.get(ExecutionRun, parent['id'])
    assert current.status == 'completed'
    data = current.output_data['groups'][0]['output']['prepared']
    assert data['project']['check_item_no'] == '5103.426'
    assert data['values']['title'] == '纤维横截面原始记录'
    assert data['values']['sample_name'] == '人工简名'
    assert data['template_key'] == f'cross-section-{count}'
    assert [item['source']['id'] for item in data['files']] == [item['id'] for item in reversed(selected)]
    assert env.db.query(ExecutionHumanTask).count() == 1
    assert env.db.query(ExecutionExternalOperation).count() == 0


@pytest.mark.skipif(os.getenv('EXECUTION_RUN_UNO_INTEGRATION_TESTS') != '1', reason='Requires Worker UNO')
def test_exported_cross_section_worker_upload_review_and_excel_entry(operation_env, monkeypatch):
    env = operation_env
    monkeypatch.setattr(settings, 'EXECUTION_LEGACY_SPECIAL_WOOL_WRITE_ENABLED', True)
    image_files(env, 3)
    cache = env.db.get(ExecutionTaskSnapshotCache, NUMBER)
    snapshot = deepcopy(cache.snapshot)
    row = snapshot['projects'][0]
    row.update({key: value for key, value in project().items()
                if key not in ('project_key', 'task_check_item_id', 'check_item_id')})
    row['project_key'] = 'task-project:' + hashlib.sha256('\0'.join(str(row[key]) for key in (
        'task_check_item_id', 'check_item_id', 'check_item_no', 'check_item_name', 'check_method', 'seq_num')).encode()).hexdigest()[:24]
    cache.snapshot = snapshot
    for file in (ROOT / 'backend/app/execution/templates').glob('*.xls'):
        shutil.copyfile(file, Path(env.roots['execution_templates'].local_path) / file.name)
    share = env.path / 'cross-section-share'
    share.mkdir()
    root = ExecutionStorageRoot(root_id='report_upload_images', name='Share', local_path=str(share),
        source_uri=r'\\server\share', access_mode='write', is_available=True, is_active=True)
    env.db.add(root)
    env.db.commit()
    env.roots[root.root_id] = root
    doc = json.loads(DOCUMENTS[1].read_text())
    next(n for n in doc['definition']['nodes'] if n['id'] == 'task')['input_mapping']['refresh'] = False
    workflow = publish_native_document(env.db, env.admin, doc, {
        'root_slots': {slot['slot_id']: {'root_id': slot['slot_id'], 'revision': 1} for slot in doc['resources']['root_slots']},
        'credential_slots': {'inspection': {'credential_id': env.credential.id, 'revision': 1}}})
    parent = run(env, workflow.id, inputs={'inspection_number': NUMBER, 'relative_directory': ''})
    drain(env)
    env.db.expire_all()
    task = env.db.query(ExecutionHumanTask).filter_by(run_id=parent['id'], status='open').one()
    selected = env.db.get(ExecutionNodeRun, task.node_run_id).input_data['items']
    submit_human_task(env.db, task_id=task.id, actor=env.admin, expected_revision=task.revision,
        data={'groups': [{'id': '1', 'selected_ids': [item['id'] for item in reversed(selected)]}],
              'form_data': {'sample_name': '人工简名'}})
    env.db.commit()
    advance(env)
    for ref, receipt in [('legacy_fibrecheck.original_record.upload@1', upload_receipt),
                         ('legacy_fibrecheck.original_record.review@1', review_receipt),
                         ('legacy_fibrecheck.check_record.excel_entry@1', excel_receipt)]:
        env.db.expire_all()
        pending = env.db.query(ExecutionExternalOperation).filter_by(status='approved').one()
        operation, attempt, _ = claim_approved_external_operation(env.db, bridge_id='test',
            account_name='test-operator', supported_operation_types={ref, pending.request_summary['operation_type']})
        record_external_attempt_stage(env.db, attempt_id=attempt.id, bridge_id='test', stage=_operation_stage_profile(operation)[2])
        env.db.commit()
        complete_external_attempt(env.db, attempt_id=attempt.id, bridge_id='test', receipt=receipt(operation))
        env.db.commit()
        advance(env)
    advance(env)
    env.db.expire_all()
    current = env.db.get(ExecutionRun, parent['id'])
    assert current.status == 'completed'
    result = current.output_data['groups'][0]['output']
    assert result['submitted'] is True
    artifact = result['original']['artifact']
    book = xlrd.open_workbook(Path(env.roots[artifact['root_id']].local_path) / artifact['relative_path'])
    try:
        sheet = book.sheet_by_name('微观形貌')
        assert sheet.cell_value(0, 0) == '纤维横截面原始记录'
        assert sheet.cell_value(1, 4) == 'GB/T 36422-2018'
        assert sheet.cell_value(2, 1) == '人工简名'
    finally:
        book.release_resources()
    directory = share / f'数据分析中心/3-报告上传图片/8-材料检测中心/1-微观形貌-GB T 36422/{NUMBER}'
    for index, source in enumerate(reversed(selected)):
        target = directory / f'{NUMBER}-横截面{("-" + str(index)) if index else ""}.png'
        original = Path(env.roots[source['root_id']].local_path) / source['relative_path']
        assert target.read_bytes() == original.read_bytes()
    entry = env.db.query(ExecutionExternalOperation).order_by(ExecutionExternalOperation.created_at).all()[-1]
    assert entry.request_summary['template_binding']['template_name'] == '纤维横截面-3张图.xls'
    assert env.db.query(ExecutionExternalOperation).count() == 3
    assert env.db.query(ExecutionProjectRule).count() == 0
