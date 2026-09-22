"""Copied into JSON: fields, copy names and registration values are visible."""
import re


def main(inputs):
    project = inputs['project']['metadata']['project']
    form, rules = inputs['form'], inputs['rules']
    number = inputs['inspection_number']
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', number):
        raise ValueError('编号包含文件名不允许的字符')
    if not form.get('sample_name', '').strip():
        raise ValueError('请填写样品名称')
    identity = form.get('sample_identity', '').strip()
    fields = {'inspection_number': number, 'sample_name': form['sample_name'].strip(),
              'sample_identity': identity, 'remark': form.get('remark', '').strip(),
              'method': project['check_method'], 'item_name': project['check_item_name'],
              'title': rules['record_title']}
    needs_judgement = str(project.get('give_judgement') or '').strip().casefold() not in ('', '0', 'false', 'no', '否')
    for key in ('judge_basis', 'indicator_requirement', 'test_result', 'judgement'):
        fields[key] = form.get(key, '').strip() if needs_judgement else ''
        if needs_judgement and not fields[key]:
            raise ValueError('任务要求判定，请补充全部判定字段')
    key = str(len(inputs['images']))
    template = inputs['templates'].get(key)
    if template is None:
        raise ValueError('当前图片数量没有配置登记模板')
    safe_identity = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', identity).rstrip('. ').strip()
    stem = number + ('-' + safe_identity if safe_identity else '')
    files, images = [], []
    for index, image in enumerate(inputs['images']):
        source = {k: image[k] for k in ('id', 'root_id', 'relative_path', 'fingerprint')}
        suffix = '.' + source['relative_path'].rsplit('.', 1)[-1].lower()
        name = stem + ('-' + str(index) if index else '') + suffix
        files.append({'source': source, 'target_filename': name})
        images.append({'artifact': source})
    keys = ('project_key', 'task_check_item_id', 'check_item_id', 'check_item_no', 'check_item_name', 'check_method', 'seq_num', 'check_count')
    return {'inspection_number': number, 'project': {k: project[k] for k in keys}, 'values': fields,
            'images': images, 'files': files, 'target_directory': rules['target_directory'].rstrip('/') + '/' + number,
            'template_key': key, 'template_binding': template,
            'registration_template': {'template_name': template['legacy_template_name'], 'mapping_config_sha256': template['mapping_config_sha256']},
            'register': {'level': '', 'sample_identity': identity, 'equipment_no': '', 'check_basis': ''},
            'expected_key_identities': [identity], 'business_fields': rules['upload_fields'],
            'expected_existing_register_count': project['register_count']}
