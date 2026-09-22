"""Copied verbatim into the workflow's editable Python node."""
import re
import unicodedata


def clean(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def main(inputs):
    if inputs['truncated']:
        raise ValueError('图片超过查询上限，请填写更具体的相对目录后重新查询')
    number = clean(inputs['inspection_number']).upper()
    rules = inputs['rules']
    pattern = r'(?<![A-Z0-9])' + re.escape(number) + r'(?![A-Z0-9])'
    items = []
    for source in inputs['items']:
        path = clean(source['relative_path']).replace('\\', '/')
        if not re.search(pattern, path.upper()):
            continue
        item = dict(source)
        item['kind'] = 'image'
        item['metadata'] = dict(source.get('metadata') or {})
        items.append(item)
    items.sort(key=lambda item: [int(part) if part.isdigit() else part.casefold()
                                for part in re.split(r'(\d+)', item['relative_path'])])
    if not items:
        raise ValueError('未找到匹配编号的图片，请核对图片目录和文件索引')
    projects = []
    for project in inputs['snapshot']['projects']:
        matches = (clean(project.get('check_item_no')) in rules['project_numbers']
                   or clean(project.get('check_item_name')) in rules['project_names'])
        if not matches or clean(project.get('check_method')) not in rules['methods']:
            continue
        projects.append({'id': project['project_key'], 'kind': 'option',
                         'label': str(project.get('seq_num', '')) + ' · ' + project['check_item_name'] + ' · ' + project['check_method'],
                         'fingerprint': project['project_key'], 'metadata': {'project': project}})
    if not projects:
        raise ValueError('任务没有符合项目与方法规则的检测项目')
    counts = sorted(int(key) for key in inputs['template_counts'])
    return {'inspection_number': number, 'items': items, 'projects': projects,
            'allowed_selected_counts': counts}
