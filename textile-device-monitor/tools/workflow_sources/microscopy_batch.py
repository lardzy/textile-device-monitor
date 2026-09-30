"""Freeze the selected group payloads for the generic sequential batch node."""


def main(inputs):
    name = inputs['form_data']['sample_name'].strip()
    if not name:
        raise ValueError('请选择或填写样品名称')
    return {'items': [{'key': group['id'], 'label': group['label'], 'inputs': {
        'execution_group': {'choice_id': group['metadata']['choice_id'],
                            'sample_name': name,
                            'sample_identity': group['metadata']['sample_identity'],
                            'images': group['selected_items']}}}
        for group in inputs['groups']]}
