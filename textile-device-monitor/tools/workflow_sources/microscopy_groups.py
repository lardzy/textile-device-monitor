"""Task sample identifiers become selection groups, never business profiles."""
import re
import unicodedata


def name_candidates(snapshot, separator, tokens):
    raw = snapshot.get('sample_names') or snapshot.get('sample_name') or []
    values = raw if isinstance(raw, list) else [raw]
    originals = list(dict.fromkeys(str(value).strip() for value in values if value))
    full = [' '.join(unicodedata.normalize('NFKC', value).split()) for value in originals]
    # Keep complete task names and phrases, then offer generic Chinese tokens.
    # A suggestion is never an automatic choice; the operator can rewrite it.
    parts = [part.strip() for value in originals
             for phrase in re.split(separator, unicodedata.normalize('NFKC', value))
             for part in re.split(r'[\r\n,，、;；/\\|]+', phrase)]
    words = [part.strip() for value in parts for part in re.split(r'[\s()\[\]【】]+', value)]
    candidates = list(dict.fromkeys(value for value in full + parts + tokens + words
                                    if value and value != '---' and len(value) <= 500))[:50]
    return '\n'.join(originals), candidates


def main(inputs):
    choice = inputs['project']
    project, profile = choice['metadata']['project'], choice['metadata']['profile']
    raw = project.get('sample_identify') or ''
    values = raw if isinstance(raw, list) else [raw]
    identities = list(dict.fromkeys(unicodedata.normalize('NFKC', part).strip()
        for value in values for part in re.split(profile['form_rules']['identity_separator'], str(value))
        if part.strip() and part.strip() != '---')) or ['']
    counts = sorted(int(key) for key in profile['templates'])
    groups = [{'id': str(index + 1), 'label': identity or '本次样品（无样品识别）',
               'metadata': {'sample_identity': identity, 'choice_id': choice['id']},
               'allowed_selected_counts': counts} for index, identity in enumerate(identities)]
    original, candidates = name_candidates(inputs['snapshot'], profile['form_rules']['name_separator'], inputs['name_tokens'])
    schema = {'type': 'object', 'title': '各组共用的样品名称',
              'description': '点击下方分词建议填入名称，也可直接输入或继续修改。本次所有分组使用此名称生成原始记录。',
              'properties': {'sample_name': {'type': 'string', 'title': '写入原始记录的样品名称',
                  'minLength': 1, 'maxLength': 500, 'pattern': r'\S', 'default': '',
                  'placeholder': '可点击上方分词，也可直接输入名称', 'x-suggestions': candidates,
                  'x-suggestion-display': 'buttons', 'x-suggestion-label': '样品名称分词建议'}},
              'required': ['sample_name'], 'additionalProperties': False}
    return {'groups': groups, 'allowed_selected_counts': counts, 'form_schema': schema,
            'context': {'任务单样品名称': original or '任务单未提供，请人工填写'}}
