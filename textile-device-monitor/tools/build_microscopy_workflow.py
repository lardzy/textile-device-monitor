"""Development compiler: every business rule and Python source travels in JSON."""
from copy import deepcopy
import json
from pathlib import Path

from build_paper_workflow import RESOURCE, S, O, A, obj, node, output, python_node, PRESETS
from app.execution.v2.designer import starter_document, compile_document
from app.execution.v2.registry import get_installed_registry

SOURCES = Path(__file__).with_name('workflow_sources')


def build():
    PRESETS.clear()
    registry = get_installed_registry()
    document = starter_document()
    document['release'].update(slug='fiber-microscopy-v2', name='纤维微观形貌', category_key='other',
        description='GB/T 36422-2018；选图、记录生成、共享图片及检务登记；规则可在 Python 节点编辑。')
    document['definition']['input_schema']['properties']['relative_directory'] = {
        'type': 'string', 'title': '图片相对目录（可选）', 'default': ''}
    document['resources'] = {'root_slots': [{'slot_id': 'electron_microscopy_records', 'name': '显微图片目录', 'access': 'read', 'required': True}],
                             'credential_slots': [], 'rule_slots': [], 'role_slots': []}
    query = node('task', '查询检务任务', 'connector.query',
        {'query_ref': 'legacy_fibrecheck.task_snapshot.get@1', 'wait_until_ready': True, 'wait_timeout_seconds': 120, 'retry_interval_seconds': 2},
        {'inspection_number': '$.inputs.inspection_number', 'refresh': True}, 2)
    files = node('files', '查询图片索引', 'file.query',
        {'root_slot': 'electron_microscopy_records', 'extensions': ['.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'],
         'recent_days': None, 'limit': 1000, 'sort': 'name_asc', 'projection': ['id', 'root_id', 'relative_path', 'fingerprint', 'metadata']},
        {'inspection_number': '$.inputs.inspection_number', 'relative_directory': '$.inputs.relative_directory'}, 2)
    item_schema = deepcopy(registry.resolve_node_spec('human.select', 2).public_dict()['input_schema']['properties']['items'])
    counts = {'type': 'array', 'items': {'type': 'integer'}}
    candidates = python_node('candidates', '显微 · 匹配项目和图片', (SOURCES/'microscopy_candidates.py').read_text(),
        {'inspection_number': S, 'snapshot': O, 'items': A, 'truncated': {'type': 'boolean'}, 'rules': O, 'template_counts': {'type': 'array', 'items': {'type': 'integer'}}},
        {'inspection_number': S, 'items': item_schema, 'projects': item_schema, 'allowed_selected_counts': counts},
        {'inspection_number': '$.inputs.inspection_number', 'snapshot': output('task', 'snapshot'), 'items': output('files', 'items'),
         'truncated': output('files', 'truncated'), 'template_counts': [1, 2, 3, 5, 6, 7, 10],
         'rules': {'project_numbers': ['5103.5'], 'project_names': ['纤维微观形貌', '膜平面形貌'], 'methods': ['GB/T 36422-2018']}})
    project = node('project', '选择检测项目', 'human.select',
        {'title': '选择检测项目', 'item_kind': 'option', 'min_selected': 1, 'max_selected': 1,
         'require_primary': True, 'auto_submit_single_candidate': True}, {'items': output('candidates', 'projects')}, 2)
    select = node('images', '选择并排列图片', 'human.select',
        {'title': '选择并排列图片', 'item_kind': 'image', 'min_selected': 1, 'max_selected': 10,
         'require_primary': False, 'auto_submit_single_candidate': True},
        {'items': output('candidates', 'items'), 'allowed_selected_counts': output('candidates', 'allowed_selected_counts')}, 2)
    form_schema = {'type': 'object', 'properties': {key: S for key in ('sample_name', 'sample_identity', 'remark', 'judge_basis', 'indicator_requirement', 'test_result', 'judgement')},
                   'required': ['sample_name', 'sample_identity', 'remark'], 'additionalProperties': False}
    prepare = python_node('prepare_form', '显微 · 准备必要输入', (SOURCES/'microscopy_form.py').read_text(),
        {'snapshot': O, 'project': O, 'images': A, 'rules': O}, {'form_schema': O, 'defaults': O, 'context': O},
        {'snapshot': output('task', 'snapshot'), 'project': output('project', 'primary_item'), 'images': output('images', 'selected_items'),
         'rules': {'name_separator': r'[\r\n;；]+', 'identity_separator': r'[、,，;；\r\n]+',
                   'basis_separator': r'[;；\r\n]+', 'judgements': ['符合', '不符合']}})
    form = node('form', '补充必要字段', 'human.form', {'title': '补充记录字段', 'result_schema': form_schema, 'auto_submit_complete': True},
        {'form_schema': output('prepare_form', 'form_schema'), 'defaults': output('prepare_form', 'defaults'), 'context': output('prepare_form', 'context')}, 2)
    end = node('end', '查询与选择完成', 'core.end', mapping={'project': output('project', 'primary_item'), 'images': output('images', 'selected_items'), 'form': output('form')}, version=2)
    nodes = [document['definition']['nodes'][0], query, files, candidates, project, select, prepare, form, end]
    for index, item in enumerate(nodes):
        item['ui'] = {'x': 40 + index * 260, 'y': 160}
    document['definition'].update(nodes=nodes, edges=[{'id': a['id']+'-'+b['id'], 'source': a['id'], 'target': b['id'], 'join_policy': 'all'} for a, b in zip(nodes, nodes[1:])],
        output_schema=obj({'project': O, 'images': A, 'form': O}))
    result = compile_document(document)
    if not result['content_valid']:
        raise ValueError(json.dumps(result['issues'], ensure_ascii=False, indent=2))
    return result['document']


if __name__ == '__main__':
    document = build()
    (RESOURCE/'workflows/fiber-microscopy-v2.json').write_text(json.dumps(document, ensure_ascii=False, indent=2)+'\n')
    print('Built editable microscopy workflow')
