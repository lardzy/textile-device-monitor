"""Graph validation applies to native JSON and structured references only."""
from copy import deepcopy

import pytest
from app.execution.v2.data_examples import build_data_python_release
from app.execution.v2.designer import compile_document, starter_document
from app.execution.registry import node_registry


def codes(document):
    return {issue['code'] for issue in compile_document(document)['issues']}


def test_valid_native_dag_and_empty_legacy_registry():
    assert not node_registry.all()
    assert compile_document(starter_document())['content_valid']


def test_cycle_is_rejected():
    doc=starter_document();doc['definition']['edges'].append({'id':'cycle','source':'end','target':'start','join_policy':'all'})
    assert 'cycle_forbidden' in codes(doc)


def test_unknown_version_is_rejected():
    doc=starter_document();doc['definition']['nodes'][1]['type_version']=999
    from app.execution.errors import ExecutionApiError
    with pytest.raises(ExecutionApiError,match='not installed'):compile_document(doc)


@pytest.mark.parametrize('expression,expected',[
    ('$.inputs.missing','mapping_source_path_unknown'),
    ('$.nodes.missing.output.total','mapping_source_node_missing'),
    ('$.nodes.end.output.total','mapping_source_not_upstream'),
    ('$.inputs.inspection_number','mapping_type_mismatch'),
])
def test_nested_input_mapping_references_are_typed_and_upstream(expression,expected):
    doc=build_data_python_release();doc['definition']['nodes'][1]['input_mapping']['values']=expression
    assert expected in codes(doc)


def test_branch_condition_must_exist_and_be_available_at_source():
    doc=build_data_python_release();edge=doc['definition']['edges'][0]
    edge['condition']={'path':'$.nodes.compute.output.total','operator':'gt','value':0}
    assert 'mapping_source_not_upstream' in codes(doc)
    edge['condition']['path']='$.nodes.deleted.output.total'
    assert 'mapping_source_node_missing' in codes(doc)


def test_python_source_and_fixture_values_are_not_references():
    doc=build_data_python_release()
    doc['definition']['nodes'][1]['config']['code'] += '\n# $.nodes.deleted.output.total\n'
    doc['fixtures'][0]['assertions'].append({'path':'$.run.status','operator':'ne','value':'$.nodes.deleted.output.value'})
    assert compile_document(doc)['content_valid']


def test_deleted_node_assertion_stays_invalid_until_repaired():
    doc=build_data_python_release();doc['definition']['nodes'].pop(1)
    doc['definition']['nodes'][-1]['input_mapping']={'total':0}
    doc['definition']['edges']=[{'id':'end','source':'start','target':'end','join_policy':'all'}]
    doc['fixtures'][0]['assertions']=[{'path':'$.nodes.compute.error_code','operator':'eq','value':'test'}]
    assert 'mapping_source_node_missing' in codes(doc)
