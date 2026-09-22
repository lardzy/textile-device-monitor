"""Build the editable paper example and copy-on-add Python presets.

This is a development helper, never a runtime dispatch table. Every Python body,
business parameter, Schema and cell address is embedded in the resulting JSON.
Run with PYTHONPATH=backend after execution_v2_pack_freeze.py --update.
"""
from copy import deepcopy
import json
from pathlib import Path
from textwrap import dedent
from app.execution.v2.designer import starter_document, compile_document
from app.execution.v2.registry import get_installed_registry

RESOURCE = Path(__file__).resolve().parents[1] / 'backend/app/execution/v2/resources'
S = {'type': 'string'}
O = {'type': 'object', 'additionalProperties': True}
A = {'type': 'array', 'items': O}

def obj(properties):
    return {'type':'object', 'properties':properties, 'required':list(properties), 'additionalProperties':False}

PRESETS = []
def python_node(identifier, title, code, inputs, outputs, mapping):
    config = {'runtime_version':1, 'timeout_seconds':5, 'code':dedent(code).strip()+'\n', 'input_schema':obj(inputs), 'output_schema':obj(outputs)}
    PRESETS.append({'id':identifier, 'name':title, 'config':deepcopy(config)})
    return node(identifier, title, 'data.python', config, mapping)

def node(identifier, title, kind, config=None, mapping=None, version=1):
    return {'id':identifier,'name':title,'type':kind,'type_version':version,'config':config or {},'input_mapping':mapping or {}}

def output(identifier, field=''):
    return '$.nodes.'+identifier+'.output'+('.'+field if field else '')

def build():
    PRESETS.clear()
    registry = get_installed_registry()
    document = starter_document()
    document['release'].update(slug='paper-fiber-v2',name='纸浆纤维鉴别',category_key='other',description='原始 Excel 读取、必要选择、上传复核及登记；全部业务规则可编辑。')
    document['resources']={'root_slots':[{'slot_id':'paper_fiber_records','name':'纸浆原始记录目录','access':'read','required':True}], 'credential_slots':[{'slot_id':'inspection','name':'检务账号','connector_id':'legacy_fibrecheck','credential_kind':'password','required':True}], 'rule_slots':[], 'role_slots':[]}
    query=node('task','查询检务任务','connector.query',{'query_ref':'legacy_fibrecheck.task_snapshot.get@1','wait_until_ready':True,'wait_timeout_seconds':120,'retry_interval_seconds':2},{'inspection_number':'$.inputs.inspection_number','refresh':True},2)
    files=node('files','查询文件索引','file.query',{'root_slot':'paper_fiber_records','extensions':['.xls','.xlsx'],'recent_days':None,'limit':1000,'sort':'modified_desc','projection':['id','root_id','relative_path','name','suffix','size','modified_at','fingerprint','metadata']},{'inspection_number':'$.inputs.inspection_number'},2)
    filtered=python_node('filter_files','纸浆 · 筛选文件',r'''
        import re
        import unicodedata
        def main(inputs):
            if inputs['truncated']:
                raise ValueError('文件超过查询上限，请缩小目录范围后重新查询')
            number = unicodedata.normalize('NFKC', inputs['inspection_number']).strip().upper()
            rules = inputs['rules']
            expression = r'(?<![A-Z0-9])' + re.escape(number) + r'(?![A-Z0-9])'
            items = []
            for item in inputs['items']:
                path = unicodedata.normalize('NFKC', item['relative_path']).replace('\\', '/').upper()
                if not re.search(expression, path):
                    continue
                if any(word.upper() in path for word in rules['exclude_words']):
                    continue
                items.append(item)
            if not items:
                raise ValueError('目录中没有匹配该编号的 Excel；请核对目录与文件索引')
            return {'sources': items, 'inspection_number': number}
    ''',{'inspection_number':S,'items':A,'truncated':{'type':'boolean'},'rules':obj({'exclude_words':{'type':'array','items':S}})}, {'sources':A,'inspection_number':S}, {'inspection_number':'$.inputs.inspection_number','items':output('files','items'),'truncated':output('files','truncated'),'rules':{'exclude_words':['~$']}})
    read=node('read','批量读取原始 Excel','workbook.extract_fields',{'data_only':True,'fields':[{'name':'result','sheet':'Sheet1','cell':'W32','required':True},{'name':'standard','sheet':'Sheet1','cell':'M32','required':False}]},{'sources':output('filter_files','sources')},2)
    candidate_schema=deepcopy(registry.resolve_node_spec('human.select',2).public_dict()['input_schema']['properties']['items'])
    candidates=python_node('candidates','纸浆 · 整理候选',r'''
        import re
        import unicodedata
        def clean(value):
            return unicodedata.normalize('NFKC', str(value if value is not None else '')).strip()
        def main(inputs):
            rules = inputs['rules']
            projects = [p for p in inputs['snapshot']['projects']
                        if clean(p['check_item_name']) in rules['project_names']
                        and (not rules['methods'] or clean(p['check_method']) in rules['methods'])]
            if not projects:
                raise ValueError('检务任务没有符合配置的检测项目，请检查项目名称和方法规则')
            items, errors = [], []
            for row in inputs['rows']:
                if row['read_status'] != 'succeeded':
                    errors.append({'source': row['source'], 'errors': row['errors']})
                    continue
                result = clean(row['values'].get('result'))
                if not result:
                    errors.append({'source': row['source'], 'errors': ['结果字段为空']})
                    continue
                unit = rules['percent_unit'] if re.search(rules['percent_pattern'], result) else ''
                for project in projects:
                    file = row['file']
                    items.append({'id': file['id'] + ':' + project['project_key'], 'kind':'artifact',
                        'label': file['label'] + ' · ' + project['check_item_name'] + ' · ' + result,
                        'root_id':row['source']['root_id'], 'relative_path':row['source']['relative_path'],
                        'fingerprint':row['sha256'], 'items':[file], 'metadata':{'project':project,'source':row['source'],
                            'sha256':row['sha256'], 'format':row['format'], 'raw_values':row['values'],
                            'result':result,'standard':clean(row['values'].get('standard')),'unit':unit}})
            if not items:
                raise ValueError('没有可用的非空工作簿结果；读取错误：' + str(errors))
            return {'items':items,'read_errors':errors}
    ''', {'snapshot':O,'rows':A,'rules':obj({'project_names':{'type':'array','items':S},'methods':{'type':'array','items':S},'percent_pattern':S,'percent_unit':S})}, {'items':candidate_schema,'read_errors':A}, {'snapshot':output('task','snapshot'),'rows':output('read','items'),'rules':{'project_names':['纸、纸板和纸浆纤维鉴别分析'],'methods':['GB/T 4688-2020'],'percent_pattern':r'(?<![\w.])100(?:\.0+)?(?![\w.])','percent_unit':'%'}})
    select=node('select','选择原始记录与项目','human.select',{'title':'选择原始记录与项目','item_kind':'artifact','min_selected':1,'max_selected':1,'require_primary':True,'auto_submit_single_candidate':True},{'items':output('candidates','items'),'context':{'读取错误':output('candidates','read_errors')}},2)
    form_properties={key:{**S,'title':title} for key,title in [('sample_identity','样品标识'),('judge_basis','判定依据'),('judgement','判定结果'),('standard_value','标准值')]}
    form_schema=obj(form_properties)
    prepare=python_node('prepare_form','纸浆 · 准备必要输入',r'''
        import re
        import unicodedata
        def main(inputs):
            project = inputs['selected']['metadata']['project']
            raw = unicodedata.normalize('NFKC', str(project.get('sample_identify') or '')).strip()
            choices = list(dict.fromkeys([x.strip() for x in re.split(inputs['rules']['identity_separator'],raw) if x.strip()]))
            fields = {'sample_identity':{'type':'string','title':'样品标识'},
                      'judge_basis':{'type':'string','title':'判定依据'},
                      'judgement':{'type':'string','title':'判定结果'},
                      'standard_value':{'type':'string','title':'标准值'}}
            defaults = dict.fromkeys(fields,'')
            if len(choices) == 1:
                defaults['sample_identity'] = choices[0]
            if choices:
                fields['sample_identity']['enum'] = choices
            if len(choices) > 1 or project['check_count'] > 1:
                fields['sample_identity']['minLength'] = 1
            if project.get('give_judgement'):
                for key in ('judge_basis','judgement','standard_value'):
                    fields[key]['minLength'] = 1
                defaults['standard_value'] = inputs['selected']['metadata']['standard']
                fields['judgement']['enum'] = inputs['rules']['judgements']
            return {'form_schema':{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False},
                    'defaults':defaults,'context':{'选择的记录':inputs['selected']['label'], '原始结果':inputs['selected']['metadata']['result']}}
    ''',{'selected':O,'rules':obj({'identity_separator':S,'judgements':{'type':'array','items':S}})}, {'form_schema':O,'defaults':form_schema,'context':O}, {'selected':output('select','primary_item'),'rules':{'identity_separator':r'[、,，;；\r\n]+','judgements':['合格','不合格']}})
    form=node('form','补充必要字段','human.form',{'title':'补充登记字段','result_schema':form_schema,'auto_submit_complete':True},{'form_schema':output('prepare_form','form_schema'),'defaults':output('prepare_form','defaults'),'context':output('prepare_form','context')},2)
    entry_contract=registry.connectors.resolve_operation('legacy_fibrecheck','*','check_record.generic_entry',2).spec['input_schema']
    upload_contract=registry.connectors.resolve_operation('legacy_fibrecheck','*','original_record.upload',1).spec['input_schema']
    result_props={key:deepcopy(upload_contract['properties'][key]) for key in ['inspection_number','project','source','sha256','business_fields']}
    result_props.update({key:deepcopy(entry_contract['properties'][key]) for key in ['record','expected_existing_register_count']})
    registration=python_node('registration','纸浆 · 组装登记数据',r'''
        def main(inputs):
            data = inputs['selected']['metadata']
            project, form = data['project'], inputs['form']
            if project.get('give_judgement') and not all(form[key].strip() for key in ('judge_basis','judgement','standard_value')):
                raise ValueError('本项目要求判定，请补充依据、结果和标准值')
            header = dict.fromkeys(('grade','unit','judge_basis','test_method','sample_description','standard_type','report_check_item_name','attach_info','remark','total_judge'),'')
            header.update(unit=data['unit'], test_method=project['check_method'],sample_description=form['sample_identity'])
            if project.get('give_judgement'):
                header.update(judge_basis=form['judge_basis'],total_judge=form['judgement'])
            detail = {'standard_location':'','standard_value':form['standard_value'] if project.get('give_judgement') else '', 'real_location':'','real_value':data['result']}
            keys = ('project_key','task_check_item_id','check_item_id','check_item_no','check_item_name','check_method','seq_num','check_count')
            return {'inspection_number':inputs['inspection_number'],'project':{key:project[key] for key in keys},
                    'source':data['source'],'sha256':data['sha256'],'business_fields':inputs['business_fields'],
                    'expected_existing_register_count':project['register_count'], 'record':{'header':header,'details':[detail]}}
    ''',{'inspection_number':S,'selected':O,'form':form_schema,'business_fields':upload_contract['properties']['business_fields']}, result_props, {'inspection_number':output('filter_files','inspection_number'),'selected':output('select','primary_item'),'form':output('form'),'business_fields':{'fiber_category':'棉再生纤','inspection_method':'定量','inspection_item':'棉再生纤定性','inspection_copies':1,'review_item':'棉再生纤定性','review_copies':1,'file_type':'定量试验'}})
    upload=node('upload','上传原始 Excel','external.operation',{'operation_ref':'legacy_fibrecheck.original_record.upload@1','credential_slot':'inspection'},{key:output('registration',key) for key in ['inspection_number','project','source','sha256','business_fields']})
    review=node('review','复核原始记录','external.operation',{'operation_ref':'legacy_fibrecheck.original_record.review@1','credential_slot':'inspection'},{'upload_result':output('upload')})
    entry=node('entry','登记检务结果','external.operation',{'operation_ref':'legacy_fibrecheck.check_record.generic_entry@2','credential_slot':'inspection'},{**{key:output('registration',key) for key in ['inspection_number','project','record','expected_existing_register_count']},'review_result':output('review')})
    end=node('end','完成','core.end',mapping={'upload':output('upload'),'review':output('review'),'entry':output('entry')},version=2)
    nodes=[document['definition']['nodes'][0],query,files,filtered,read,candidates,select,prepare,form,registration,upload,review,entry,end]
    for index,item in enumerate(nodes): item['ui']={'x':40+index*260,'y':160}
    document['definition'].update(nodes=nodes,edges=[{'id':a['id']+'-'+b['id'],'source':a['id'],'target':b['id'],'join_policy':'all'} for a,b in zip(nodes,nodes[1:])],output_schema=obj({'upload':O,'review':O,'entry':O}))
    result=compile_document(document)
    if not result['content_valid']: raise ValueError(json.dumps(result['issues'],ensure_ascii=False,indent=2))
    return result['document']

if __name__ == '__main__':
    doc=build()
    (RESOURCE/'workflows').mkdir(exist_ok=True)
    for path,value in [(RESOURCE/'workflows/paper-fiber-v2.json',doc),(RESOURCE/'python-presets.json',PRESETS)]:
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print('Built portable paper workflow and',len(PRESETS),'copy-on-add Python presets')
