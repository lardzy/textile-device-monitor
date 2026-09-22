from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest
import xlrd
from PIL import Image

from app.execution.errors import ExecutionApiError
from app.execution.models import ExecutionStorageRoot
from app.execution.v2.native_handlers import _batch_place, NodeExecutionResult
from app.execution.v2.workbook_render import render_file
from app.execution.v2.designer import compile_document
from app.execution.workbook_images import layout_images, render_image_workbook
from native_io_helpers import image_files
from workflow_native_helpers import environment, stage, publish
from test_execution_microscopy_json import document, trial


def payload():
    project={'project_key':'p','task_check_item_id':'task','check_item_id':'item','check_item_no':'5103.5',
             'check_item_name':'纤维微观形貌','check_method':'GB/T 36422-2018','seq_num':1,'check_count':4,'register_count':1,'give_judgement':0}
    return {'inspection_number':'260191178','project':{'metadata':{'project':project}},
            'form':{'sample_name':'完整样品','sample_identity':'正/反面','remark':'备注'},
            'images':[{'id':str(i),'root_id':'images','relative_path':f'a/{i}.bmp','fingerprint':'1:2'} for i in range(3)]}


def test_copy_names_fields_and_template_choice_are_portable():
    result=trial('payload',**payload())
    assert result['passed'], result
    data=result['output']
    assert data['template_key']=='3'
    assert [f['target_filename'] for f in data['files']]==['260191178-正-反面.bmp','260191178-正-反面-1.bmp','260191178-正-反面-2.bmp']
    assert data['values']['sample_identity']=='正/反面'
    assert data['values']['judgement']==''
    assert data['target_directory'].endswith('/260191178')


@pytest.mark.parametrize('count',[1,2,3,5,6,7,10])
def test_every_registration_template_keeps_literal_feed_cells(tmp_path,count):
    node=next(n for n in document()['definition']['nodes'] if n['id']=='check')
    reference=node['config']['templates'][str(count)]
    template=Path(__file__).parents[1]/'app/execution/templates'/reference['relative_path']
    values=trial('payload',**payload())['output']['values']
    target=tmp_path/'output.xls'
    result=render_file(template,target,values=values,fields=node['config']['fields'])
    assert result['cells_verified']==16
    book=xlrd.open_workbook(target)
    try:
        assert book.sheet_by_name('Sheet1').cell_value(6,60)=='纤维微观形貌' # BI7
        assert book.sheet_by_name('Sheet1').cell_value(6,62)=='正/反面' # BK7
    finally:book.release_resources()


@pytest.mark.skipif(os.getenv('EXECUTION_RUN_UNO_INTEGRATION_TESTS') != '1', reason='Requires Worker UNO')
@pytest.mark.parametrize('count', [1, 2, 3, 5, 6, 7, 10])
def test_generic_original_record_reopens_all_image_counts(tmp_path, count):
    node = next(n for n in document()['definition']['nodes'] if n['id'] == 'original')
    config = node['config']
    template = Path(__file__).parents[1] / 'app/execution/templates' / config['templates']['original']['relative_path']
    selected = []
    for index in range(count):
        source = tmp_path / f'{index}.png'
        Image.new('RGB', (160, 100) if index % 2 == 0 else (100, 160), 'white').save(source)
        selected.append((str(index), source))
    long_name = '长样品名称及全角字符ＡＢＣ' * 8
    result = render_image_workbook(template, tmp_path / 'original.xls', cells={'B2': '260191178', 'B3': long_name},
        selected=selected, layout=config['image_layout'], number_formats=config.get('number_formats'))
    assert result['images_written'] == count
    assert result['geometry']['verified']
    assert result['uno']['print_area_verified']
    book = xlrd.open_workbook(tmp_path / 'original.xls')
    try:
        assert book.sheet_by_name(config['image_layout']['sheet']).cell_value(2, 1) == long_name
    finally:
        book.release_resources()


def placement_context(env):
    from app.execution.v2.native_handlers import _file_query
    image_files(env,3)
    root=ExecutionStorageRoot(root_id='report_upload_images',name='共享图片',local_path=str(env.path/'share'),source_uri=r'\\server\share',access_mode='write',is_active=True,is_available=True)
    Path(root.local_path).mkdir();env.db.add(root);env.db.commit();env.roots[root.root_id]=root
    ctx=SimpleNamespace(db=env.db,run=SimpleNamespace(inspection_number='N'),node={'type_version':2,'config':{'root_id':'electron_microscopy_records','limit':100}},input_data={})
    files=_file_query(ctx)['items']
    ctx.node={'type_version':2,'config':{'target_root_id':root.root_id}}
    ctx.input_data={'target_directory':'arbitrary/folder','files':[{'source':{k:f[k] for k in ('id','root_id','relative_path','fingerprint')},'target_filename':f'{i}.png'} for i,f in enumerate(files)]}
    return ctx,Path(root.local_path)/'arbitrary/folder'


def test_generic_copy_reuses_same_content_and_scopes_overwrite(environment):
    context,directory=placement_context(environment)
    first=_batch_place(context)
    assert first['placed_count']==3 and first['reused_count']==0
    assert first['display_directory']==r'\\server\share\arbitrary\folder'
    times={p.name:p.stat().st_mtime_ns for p in directory.iterdir()}
    assert _batch_place(context)['reused_count']==3
    (directory/'1.png').write_bytes(b'different')
    pending=_batch_place(context)
    assert isinstance(pending,NodeExecutionResult)
    assert pending.suspension['renderer_payload']['conflicts']==['1.png']
    context.input_data['_native_suspension_request']={**pending.suspension['state'],'decision':'overwrite'}
    done=_batch_place(context)
    assert done['placed_count']==3 and done['reused_count']==2
    assert (directory/'0.png').stat().st_mtime_ns==times['0.png']
    assert [f['target_filename'] for f in done['placed_files']]==['0.png','1.png','2.png']


def test_generic_copy_cancel_and_changed_conflict_reprompt(environment):
    context,directory=placement_context(environment)
    _batch_place(context);(directory/'0.png').write_bytes(b'conflict')
    pending=_batch_place(context)
    context.input_data['_native_suspension_request']={**pending.suspension['state'],'decision':'overwrite'}
    (directory/'0.png').write_bytes(b'changed-again')
    updated=_batch_place(context)
    assert isinstance(updated,NodeExecutionResult)
    context.input_data['_native_suspension_request']={**updated.suspension['state'],'decision':'cancel'}
    assert _batch_place(context)['placement_cancelled']
    assert (directory/'0.png').read_bytes()==b'changed-again'
    context.input_data.pop('_native_suspension_request')
    context.input_data['files'][1]['target_filename']='0.PNG'
    with pytest.raises(ExecutionApiError):_batch_place(context)


def test_xls_image_layout_is_generic_and_budgeted():
    positions=layout_images([1.5]*12,canvas_width=18000,canvas_height=8000,gap=50)
    assert len(positions)==12
    assert all(p.x+p.width<=18000 and p.y+p.height<=8000 for p in positions)


def test_template_collection_preflight_verifies_every_bound_template(environment):
    env=environment
    source=Path(__file__).parents[1]/'app/execution/templates'
    for path in source.glob('*.xls'):shutil.copyfile(path,Path(env.roots['execution_templates'].local_path)/path.name)
    root=ExecutionStorageRoot(root_id='report_upload_images',name='共享',local_path=str(env.path/'share'),access_mode='write',is_available=True,is_active=True)
    Path(root.local_path).mkdir();env.db.add(root);env.db.commit();env.roots[root.root_id]=root
    doc=document()
    # This test isolates template bindings from the final Connector credentials.
    keep={'start','task','files','candidates','project','images','prepare_form','form','payload','original','check','place','end'}
    doc['definition']['nodes']=[n for n in doc['definition']['nodes'] if n['id'] in keep]
    doc['definition']['nodes'][-1]['input_mapping']={'files':'$.nodes.check.output'}
    doc['definition']['output_schema']={'type':'object','properties':{'files':{'type':'object'}},'required':['files'],'additionalProperties':False}
    doc['definition']['edges']=[{'id':a['id']+'-'+b['id'],'source':a['id'],'target':b['id'],'join_policy':'all'} for a,b in zip(doc['definition']['nodes'],doc['definition']['nodes'][1:])]
    doc['resources']['credential_slots']=[]
    compiled=compile_document(doc);assert compiled['content_valid'],compiled['issues']
    release=stage(env,compiled['document'])
    publish(env,release)
    target=Path(env.roots['execution_templates'].local_path)/'gbt36422-2018-microscopy-3-images-v1.xls'
    target.write_bytes(b'changed')
    response=env.client.post(f"/api/execution/v2/workflow-releases/{release['id']}/preflight",json={}).json()
    assert not response['publish_ready']
    assert any(i['code']=='template_unavailable' for i in response['issues'])
