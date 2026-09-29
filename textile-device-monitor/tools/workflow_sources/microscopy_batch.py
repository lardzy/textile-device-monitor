"""Freeze the selected group payloads for the generic sequential batch node."""


def main(inputs):
    return {'items': [{'key': group['id'], 'label': group['label'], 'inputs': {
        'execution_group': {'choice_id': group['metadata']['choice_id'],
                            'sample_identity': group['metadata']['sample_identity'],
                            'images': group['selected_items']}}}
        for group in inputs['groups']]}
