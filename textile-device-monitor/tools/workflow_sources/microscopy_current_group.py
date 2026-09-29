"""Resolve the planned group against the current task and source index."""
import re
import unicodedata


def main(inputs):
    group = inputs['group']
    matches = [project for project in inputs['projects'] if project['id'] == group['choice_id']]
    if len(matches) != 1:
        raise ValueError('该分组的项目或方法已经变化，请重新选择')
    choice = matches[0]
    project, profile = choice['metadata']['project'], choice['metadata']['profile']
    raw = project.get('sample_identify') or ''
    raw = raw if isinstance(raw, list) else [raw]
    identities = [unicodedata.normalize('NFKC', part).strip() for value in raw
                  for part in re.split(profile['form_rules']['identity_separator'], str(value)) if part.strip() and part.strip() != '---'] or ['']
    if group['sample_identity'] not in identities:
        raise ValueError('该分组的样品识别已变化，请重新选择')
    offered = {item['id']: item for item in inputs['items']}
    images = []
    for image in group['images']:
        current = offered.get(image['id'])
        if current is None or current['fingerprint'] != image['fingerprint']:
            raise ValueError('选定图片已删除或改变，请重新选图')
        images.append(current)
    if len({image['id'] for image in images}) != len(images) or str(len(images)) not in profile['templates']:
        raise ValueError('该组的图片数量不符合业务方案')
    return {'project': choice, 'images': images, 'sample_identity': group['sample_identity']}
