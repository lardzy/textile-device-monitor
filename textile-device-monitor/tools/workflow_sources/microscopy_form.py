"""Copied verbatim into the workflow's editable Python node."""
import re
import unicodedata


def clean(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())


def options(value, separator):
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(clean(part) for item in values
                             for part in re.split(separator, str(item or ''))
                             if clean(part) and clean(part) != '---'))


def main(inputs):
    project = inputs['project']['metadata']['project']
    snapshot, rules = inputs['snapshot'], inputs['rules']
    name = inputs['sample_name'].strip()
    if not name:
        raise ValueError('请选择或填写样品名称')
    identities = options(project.get('sample_identify'), rules['identity_separator'])
    fields = {'sample_name': {'type': 'string', 'title': '样品名称（所有分组共用）', 'minLength': 1, 'maxLength': 500,
                              'const': name, 'readOnly': True},
              'sample_identity': {'type': 'string', 'title': '样品识别', 'maxLength': 500},
              'remark': {'type': 'string', 'title': '备注', 'maxLength': 1000}}
    defaults = {'sample_name': name,
                'sample_identity': identities[0] if len(identities) == 1 else '', 'remark': ''}
    if identities:
        fields['sample_identity'].update(enum=identities, minLength=1)
    elif not identities:
        fields['sample_identity']['const'] = ''
    needs_judgement = str(project.get('give_judgement') or '').strip().casefold() not in ('', '0', 'false', 'no', '否')
    if needs_judgement:
        for key, title in [('judge_basis', '判定依据'), ('indicator_requirement', '指标要求'),
                           ('test_result', '测试结果'), ('judgement', '判定结果')]:
            fields[key] = {'type': 'string', 'title': title, 'minLength': 1, 'maxLength': 1000}
            defaults[key] = ''
        bases = options(project.get('check_basis_options') or snapshot.get('check_basis'), rules['basis_separator'])
        fields['judge_basis']['x-suggestions'] = bases
        defaults['judge_basis'] = bases[0] if len(bases) == 1 else ''
        fields['judgement']['enum'] = rules['judgements']
    defaults.update({key: value for key, value in rules.get('defaults', {}).items() if key in fields})
    defaults['sample_name'] = name
    if 'sample_identity' in inputs:
        identity = inputs['sample_identity']
        if identity not in (identities or ['']):
            raise ValueError('样品识别不属于当前项目')
        defaults['sample_identity'] = identity
        fields['sample_identity']['const'] = identity
        fields['sample_identity']['readOnly'] = True
    return {'form_schema': {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False},
            'defaults': defaults, 'context': {'项目': inputs['project']['label'], '图片数量': len(inputs['images'])}}
