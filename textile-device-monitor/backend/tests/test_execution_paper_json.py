"""Portable paper business code, with real native Worker and workbook I/O."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from app.execution.models import ExecutionNodeRun, ExecutionRun, ExecutionHumanTask, ExecutionTaskSnapshotCache
from app.execution.v2.data_handlers import python_test
from app.execution.v2.designer import compile_document
from workflow_native_helpers import environment, stage, publish, run, drain
from native_io_helpers import NUMBER, task_snapshot, paper_file

DOCUMENT_PATH = Path(__file__).parents[1] / 'app/execution/v2/resources/workflows/paper-fiber-v2.json'

def document():
    return json.loads(DOCUMENT_PATH.read_text())

def trial(identifier, inputs):
    node = next(n for n in document()['definition']['nodes'] if n['id'] == identifier)
    report = python_test(node['config'], {**node['input_mapping'], **inputs})
    assert report['passed'], report
    return report['output']

def selected(value='１００', judgement=0):
    project = {'project_key':'task-project:'+'a'*24, 'task_check_item_id':'sha256:'+'1'*16, 'check_item_id':'sha256:'+'2'*16,
               'check_item_no':'51.113K','check_item_name':'纸、纸板和纸浆纤维鉴别分析','check_method':'GB/T 4688-2020',
               'seq_num':17,'check_count':1,'register_count':1,'give_judgement':judgement,'sample_identify':None}
    row = {'file':{'id':'file','kind':'artifact','label':'原始.xls','fingerprint':'index-fingerprint'}, 'source':{'root_id':'paper_fiber_records','relative_path':'原始.xls'},
           'sha256':'a'*64,'format':'xls','values':{'result':value,'standard':'木浆'},'read_status':'succeeded','errors':[]}
    return trial('candidates', {'snapshot':{'projects':[project]}, 'rows':[row]})['items'][0]

@pytest.mark.parametrize('value, expected, unit', [('１００','100','%'),('1000','1000',''),('100.5','100.5',''),(100,'100','%'),('木浆、竹浆','木浆、竹浆','')])
def test_python_rules_and_registration_are_embedded(value, expected, unit):
    item=selected(value)
    assert item['metadata']['result']==expected and item['metadata']['unit']==unit
    preparation=trial('prepare_form',{'selected':item})
    data=trial('registration',{'inspection_number':NUMBER,'selected':item,'form':preparation['defaults']})
    assert data['record']['details'][0]['real_value']==expected
    assert data['record']['header']['unit']==unit
    assert data['expected_existing_register_count']==1


def test_multiple_files_projects_bad_file_and_empty_result():
    item=selected()
    project=item['metadata']['project']
    row={'file':{'id':'good','kind':'artifact','label':'good.xls','fingerprint':'index-fingerprint'},**{k:item['metadata'][k] for k in ('source','sha256','format')},'values':{'result':'木浆','standard':None},'read_status':'succeeded','errors':[]}
    bad={**deepcopy(row),'read_status':'failed','errors':['corrupt workbook']}
    result=trial('candidates',{'snapshot':{'projects':[project,{**project,'project_key':'task-project:'+'b'*24}]},'rows':[row,bad]})
    assert len(result['items'])==2 and len({i['id'] for i in result['items']})==2
    assert len(result['read_errors'])==1
    empty={**row,'values':{'result':None}}
    config=next(n['config'] for n in document()['definition']['nodes'] if n['id']=='candidates')
    report=python_test(config,{'snapshot':{'projects':[project]},'rows':[empty],'rules':next(n['input_mapping']['rules'] for n in document()['definition']['nodes'] if n['id']=='candidates')})
    assert not report['passed'] and report['error']['line']


def test_form_only_requests_missing_business_values():
    item=selected(judgement=1)
    item['metadata']['project'].update(sample_identify='正面、背面',check_count=2)
    result=trial('prepare_form',{'selected':item})
    assert result['form_schema']['properties']['sample_identity']['enum']==['正面','背面']
    for key in ('sample_identity','judge_basis','judgement','standard_value'):
        assert result['form_schema']['properties'][key]['minLength']==1
    assert result['defaults']['standard_value']=='木浆'


@pytest.mark.parametrize('extension',['.xls','.xlsx'])
def test_clean_environment_runs_portable_json_without_rules_or_legacy_nodes(environment, extension):
    env=environment
    task_snapshot(env)
    cached=env.db.get(ExecutionTaskSnapshotCache,NUMBER)
    snapshot=deepcopy(cached.snapshot)
    snapshot['projects'][0].update(check_item_no='51.113K',seq_num=17,sample_identify=None)
    cached.snapshot=snapshot;env.db.commit()
    paper_file(env,suffix=extension,value='１００')
    doc=document()
    keep={'start','task','files','filter_files','read','candidates','select','prepare_form','form','registration','end'}
    doc['definition']['nodes']=[n for n in doc['definition']['nodes'] if n['id'] in keep]
    doc['definition']['nodes'][-1]['input_mapping']={'data':'$.nodes.registration.output'}
    doc['definition']['output_schema']={'type':'object','properties':{'data':{'type':'object'}},'required':['data'],'additionalProperties':False}
    doc['definition']['edges']=[e for e in doc['definition']['edges'] if e['source'] in keep and e['target'] in keep]
    doc['definition']['edges'].append({'id':'registration-end','source':'registration','target':'end','join_policy':'all'})
    doc['resources']['credential_slots']=[]
    next(n for n in doc['definition']['nodes'] if n['id']=='task')['input_mapping']['refresh']=False
    compiled=compile_document(doc)
    assert compiled['content_valid'],compiled['issues']
    deployment=publish(env,stage(env,compiled['document']))
    record=run(env,deployment['workflow_id'],inputs={'inspection_number':NUMBER});drain(env)
    env.db.expire_all()
    nodes=env.db.query(ExecutionNodeRun).filter_by(run_id=record['id']).all()
    assert env.db.get(ExecutionRun,record['id']).status=='completed', [(n.node_id,n.status,n.error_message) for n in nodes if n.error_message]
    registration=next(n for n in nodes if n.node_id=='registration').output_data
    assert registration['record']['header']['unit']=='%'
    assert registration['record']['details'][0]['real_value']=='100'
    assert not env.db.query(ExecutionHumanTask).filter_by(run_id=record['id'],status='open').count()
