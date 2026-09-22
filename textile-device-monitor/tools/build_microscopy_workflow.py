"""Development compiler: every business rule and Python source travels in JSON."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path

from build_paper_workflow import RESOURCE, S, O, A, obj, node, output, python_node, PRESETS
from app.execution.v2.designer import starter_document, compile_document
from app.execution.v2.registry import get_installed_registry

SOURCES = Path(__file__).with_name('workflow_sources')


def build():
    PRESETS.clear()
    registry = get_installed_registry()
    templates = json.loads((SOURCES/'microscopy_templates.json').read_text())
    document = starter_document()
    document['release'].update(slug='fiber-microscopy-v2', name='纤维微观形貌', category_key='other',
        description='GB/T 36422-2018；选图、记录生成、共享图片及检务登记；规则可在 Python 节点编辑。')
    document['definition']['input_schema']['properties']['relative_directory'] = {
        'type': 'string', 'title': '图片相对目录（可选）', 'default': ''}
    document['resources'] = {'root_slots': [{'slot_id': 'electron_microscopy_records', 'name': '显微图片目录', 'access': 'read', 'required': True}],
                             'credential_slots': [{'slot_id':'inspection','name':'检务账号','connector_id':'legacy_fibrecheck','credential_kind':'password','required':True}], 'rule_slots': [], 'role_slots': []}
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
         'truncated': output('files', 'truncated'), 'template_counts': [int(key) for key in templates],
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
    place_spec = registry.resolve_node_spec('file.batch_place', 2).public_dict()
    render_spec = registry.resolve_node_spec('workbook.render', 2).public_dict()
    entry_schema = registry.connectors.resolve_operation('legacy_fibrecheck', '*', 'check_record.excel_entry', 1).spec['input_schema']['properties']
    upload_schema = registry.connectors.resolve_operation('legacy_fibrecheck', '*', 'original_record.upload', 1).spec['input_schema']['properties']
    payload = python_node('payload', '显微 · 组装字段和路径', (SOURCES/'microscopy_payload.py').read_text(),
        {'inspection_number': S, 'project': O, 'images': A, 'form': O, 'rules': O, 'templates': O},
        {'inspection_number': S, 'project': O, 'values': O, 'images': render_spec['input_schema']['properties']['images'],
         'files': place_spec['input_schema']['properties']['files'], 'target_directory': S, 'template_key': S,
         'template_binding': O, 'registration_template': entry_schema['template'], 'register': entry_schema['register'],
         'expected_key_identities': entry_schema['expected_key_identities'], 'business_fields': upload_schema['business_fields'],
         'expected_existing_register_count': entry_schema['expected_existing_register_count']},
        {'inspection_number': output('candidates', 'inspection_number'), 'project': output('project', 'primary_item'),
         'images': output('images', 'selected_items'), 'form': output('form'), 'templates': templates,
         'rules': {'record_title': '纤维微观形貌检验原始记录',
                   'upload_fields': {'fiber_category':'图片','inspection_method':'','inspection_item':'图片','review_item':'',
                                     'inspection_copies':1,'review_copies':1,'file_type':'定量试验'},
                   'target_directory': '数据分析中心/3-报告上传图片/8-材料检测中心/1-微观形貌-GB T 36422'}})
    for slot, name, access in [('execution_templates','工作簿模板目录','read'), ('execution_staging','生成工作簿目录','write'), ('report_upload_images','共享图片目标目录','write')]:
        document['resources']['root_slots'].append({'slot_id':slot,'name':name,'access':access,'required':True})
    def reference(filename):
        asset = RESOURCE.parents[1]/'templates'/filename
        return {'root_slot':'execution_templates','relative_path':filename,'sha256':hashlib.sha256(asset.read_bytes()).hexdigest()}
    def fields(mapping, sheet):
        return [{'sheet':sheet,'cell':cell,'value':{'path':'#/'+key},'kind':'text'} for cell,key in mapping.items()]
    original = node('original', '生成带图片的原始记录', 'workbook.render',
        {'templates':{'original':reference('gbt36422-2018-microscopy-original-record-v1.xls')}, 'staging_root_slot':'execution_staging',
         'filename':'纤维微观形貌-原始记录.xls',
         'fields':fields({'A1':'title','B2':'inspection_number','B3':'sample_name','L3':'sample_identity','B33':'judge_basis','I33':'indicator_requirement','B34':'test_result','I34':'judgement','B35':'remark'}, '微观形貌'),
         'image_layout':{'sheet':'微观形貌','range':'A4:L32','print_area':'$A$1:$L$37','max_width':21600,'max_height':11700,'gap':0,'biff_excel_x_scale':1.0},
         'number_formats':{'K2':{'format':'YYYY/M/D','display_pattern':r'^\d{4}/\d{1,2}/\d{1,2}$'}}},
        {'template_key':'original','values':output('payload','values'),'images':output('payload','images')},2)
    check = node('check', '生成检务登记工作簿', 'workbook.render',
        {'templates':{key:reference(value['local_asset_name']) for key,value in templates.items()}, 'staging_root_slot':'execution_staging',
         'filename':'纤维微观形貌-检务登记.xls',
         'fields':fields({'AS4':'inspection_number','Z7':'sample_identity','I8':'method','I9':'judge_basis','I10':'indicator_requirement','I11':'test_result','G12':'remark','G13':'judgement',
                          'BI7':'item_name','BK7':'sample_identity','BI8':'method','BI9':'judge_basis','BI10':'indicator_requirement','BI11':'test_result','BI12':'remark','BI13':'judgement'},'Sheet1')},
        {'template_key':output('payload','template_key'),'values':output('payload','values')},2)
    place = node('place', '放置局域网图片', 'file.batch_place', {'target_root_slot':'report_upload_images'},
        {'target_directory':output('payload','target_directory'),'files':output('payload','files')},2)
    branch = node('submit_branch', '图片放置是否取消', 'flow.branch', {'expression_version':1,'multi_match':'all'})
    original_source = {key:output('original','artifact.'+key) for key in ['root_id','relative_path']}
    check_source = {key:output('check','artifact.'+key) for key in ['root_id','relative_path']}
    upload = node('upload','上传原始记录','external.operation',{'operation_ref':'legacy_fibrecheck.original_record.upload@1','credential_slot':'inspection'},
        {**{key:output('payload',key) for key in ['inspection_number','project','business_fields']},'source':original_source,'sha256':output('original','artifact.sha256')})
    review = node('review','复核原始记录','external.operation',{'operation_ref':'legacy_fibrecheck.original_record.review@1','credential_slot':'inspection'}, {'upload_result':output('upload')})
    entry = node('entry','Excel 采集登记','external.operation',{'operation_ref':'legacy_fibrecheck.check_record.excel_entry@1','credential_slot':'inspection'},
        {**{key:output('payload',key) for key in ['inspection_number','project','expected_existing_register_count','register','expected_key_identities']},
         'template':output('payload','registration_template'),'source':check_source,'sha256':output('check','artifact.sha256'),'review_result':output('review')})
    files_result = {'original':output('original'),'check':output('check'),'placement':output('place')}
    cancelled = node('cancelled','未提交，保留生成文件','core.end',mapping={**files_result,'submitted':False,'message':'图片放置已取消，未提交检务',
        'upload':None,'review':None,'entry':None},version=2)
    end = node('end', '完成', 'core.end', mapping={**files_result,'submitted':True,'message':'图片已放置，原始记录已上传复核，Excel 登记已完成',
        'upload':output('upload'),'review':output('review'),'entry':output('entry')},version=2)
    nodes = [document['definition']['nodes'][0], query, files, candidates, project, select, prepare, form, payload, original, check, place, branch, upload, review, entry, end]
    for index, item in enumerate(nodes):
        item['ui'] = {'x': 40 + index * 260, 'y': 160}
    edges = [{'id': a['id']+'-'+b['id'], 'source': a['id'], 'target': b['id'], 'join_policy':'all'} for a,b in zip(nodes,nodes[1:])]
    next(edge for edge in edges if edge['source']=='submit_branch')['condition'] = 'default'
    edges.append({'id':'cancel-placement','source':'submit_branch','target':'cancelled','join_policy':'all',
                  'condition':{'path':output('place','placement_cancelled'),'operator':'truthy'}})
    cancelled['ui'] = {'x':branch['ui']['x']+260,'y':420}
    nodes.append(cancelled)
    receipt = {'type':['object','null'],'additionalProperties':True}
    document['definition'].update(nodes=nodes, edges=edges,
        output_schema=obj({'original':O,'check':O,'placement':O,'submitted':{'type':'boolean'},'message':S,'upload':receipt,'review':receipt,'entry':receipt}))
    result = compile_document(document)
    if not result['content_valid']:
        raise ValueError(json.dumps(result['issues'], ensure_ascii=False, indent=2))
    return result['document']


if __name__ == '__main__':
    document = build()
    (RESOURCE/'workflows/fiber-microscopy-v2.json').write_text(json.dumps(document, ensure_ascii=False, indent=2)+'\n')
    print('Built editable microscopy workflow')
