"""Task sample identifiers become selection groups, never business profiles."""
import re
import unicodedata


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
    return {'groups': groups, 'allowed_selected_counts': counts}
