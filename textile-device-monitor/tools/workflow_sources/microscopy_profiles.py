"""Portable business-profile matching; the Designer edits inputs.profiles."""
import unicodedata


def clean(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def main(inputs):
    projects = []
    seen = set()
    for profile in inputs['profiles']:
        if not profile['id'] or profile['id'] in seen:
            raise ValueError('业务方案标识为空或重复')
        seen.add(profile['id'])
        if not profile['enabled']:
            continue
        for project in inputs['snapshot']['projects']:
            number = clean(project.get('check_item_no'))
            method = clean(project.get('check_method'))
            # Codes are strings. In particular, 5103.05 is not 5103.5.
            matches = any(number == clean(rule['project_number']) and method == clean(rule['method'])
                          for rule in profile['matches'])
            if not matches:
                continue
            projects.append({
                'id': project['project_key'] + ':' + profile['id'], 'kind': 'option',
                'label': project['check_item_name'] + ' · ' + method + ' · ' + profile['name'],
                'fingerprint': project['project_key'],
                'metadata': {'project': project, 'profile': profile},
            })
    return {'projects': projects}
