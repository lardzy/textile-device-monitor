import { describe, expect, it } from 'vitest';
import { compatibleTypes, upstreamNodeIds, effectiveSchemas, graphEdges, graphNodes, parseMapping, portableEdge, schemaDefaults } from './v2Designer';

describe('v2 designer document transformations', () => {
  it('suggests only upstream nodes and compatible types, including through joins', () => {
    const definition = { edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'end' }, { source: 'c', target: 'end' }, { source: 'end', target: 'later' }] };
    expect([...upstreamNodeIds(definition, 'end')].sort()).toEqual(['a', 'b', 'c']);
    expect(compatibleTypes({ type: 'string' }, { type: 'number' })).toBe(false);
    expect(compatibleTypes({ type: 'integer' }, { type: ['number', 'null'] })).toBe(true);
    expect(compatibleTypes({}, { type: 'object' })).toBe(true);
    expect(upstreamNodeIds({ edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'a' }] }, 'a')).toEqual(new Set(['b']));
  });
  it('preserves portable port identities, geometry and edge conditions', () => {
    const node = { id: 'begin', type: 'core.start', type_version: 2, name: '开始', ui: { x: 31, y: 44 } };
    const spec = { ...node, ports: { inputs: [], outputs: [{ id: 'out' }], graph_role: 'start' } };
    const edge = { id: 'edge', source: 'begin', target: 'end', source_handle: 'out', target_handle: 'in', condition: 'default', join_policy: 'all' };
    const document = { definition: { nodes: [node], edges: [edge] } };
    expect(graphNodes(document, [spec])[0]).toMatchObject({ position: node.ui, data: { ports: spec.ports } });
    expect(graphEdges(document)[0]).toMatchObject({ sourceHandle: 'out', targetHandle: 'in', condition: 'default' });
    expect(portableEdge({ source: 'begin', target: 'end', sourceHandle: 'out', targetHandle: 'in' })).toMatchObject({ source_handle: 'out', target_handle: 'in', join_policy: 'all' });
  });
  it('uses frozen contract schemas and distinguishes a JSON value from a path', () => {
    expect(schemaDefaults({ properties: { x: { const: 1 }, y: { default: false }, z: { type: 'string' } } })).toEqual({ x: 1, y: false });
    expect(parseMapping('$.nodes.query.output.items')).toBe('$.nodes.query.output.items');
    expect(parseMapping('[1, 2]')).toEqual([1, 2]);
    expect(parseMapping('')).toBeUndefined();
    expect(effectiveSchemas({ config: {} }, { schema_bindings: { input: { source: 'workflow_input_schema' } } }, [], { input_schema: { type: 'object' } }).input).toEqual({ type: 'object' });
    const output = { type: 'object', properties: { receipt: { type: 'object' } } };
    expect(effectiveSchemas({ config: { operation_ref: 'legacy.write@1' } }, { schema_bindings: { output: { source: 'operation_spec' } } }, [{ operations: [{ operation_ref: 'legacy.write@1', output_schema: output }] }]).output).toEqual(output);
  });
});

import { deleteSelection, referenceIssues, rewriteOutputReferences, rewriteReferences, insertOnEdge, validConnection, designerHistory } from './v2Designer';
const graph = () => ({ definition: {
  nodes: [
    { id: 'a', type: 'data.python', type_version: 1, config: { output_schema: { type: 'object', properties: { result: { type: 'object', properties: { value: { type: 'string' } }, additionalProperties: false } }, additionalProperties: false } }, input_mapping: {} },
    { id: 'b', type: 'data.python', type_version: 1, config: { code: "$.nodes.a.output.result.value", input_schema: { type: 'object', properties: { payload: { type: 'array', items: { type: 'string' } } } } }, input_mapping: { payload: ['$.nodes.a.output.result.value'] } },
  ], edges: [{ id: 'ab', source: 'a', target: 'b', condition: { path: '$.nodes.a.output.result.value', operator: 'truthy' } }],
}, fixtures: [{ fixture_id: 'test', mocks: [{ node_id: 'a' }], assertions: [{ path: '$.nodes.a.output.result.value', operator: 'eq', value: 'example' }] }] });
const specs = [{ type: 'data.python', type_version: 1, schema_bindings: { input: { source: 'node_config', pointer: '/input_schema' }, output: { source: 'node_config', pointer: '/output_schema' } } }];
describe('reliable graph editing', () => {
  it('renames workflow inputs and array fields at every referenced index', () => {
    const document = graph();
    document.definition.nodes[1].input_mapping = { first: '$.nodes.a.output.rows.0.value', second: '$.nodes.a.output.rows.12.value', input: '$.inputs.number' };
    const renamed = rewriteReferences(rewriteOutputReferences(document, 'a', 'rows.*.value', 'rows.*.result'), '$.inputs', 'number', 'inspection_number');
    expect(renamed.definition.nodes[1].input_mapping).toEqual({ first: '$.nodes.a.output.rows.0.result', second: '$.nodes.a.output.rows.12.result', input: '$.inputs.inspection_number' });
  });
  it('deletes nodes and mocks together, retains broken references and assertions, restores atomically', () => {
    const initial = { document: graph(), selection: { nodes: ['a'], edges: [] } };
    const state = designerHistory({ present: initial, past: [], future: [] }, { type: 'edit', update: value => ({ ...value, document: deleteSelection(value.document, ['a']) }) });
    expect(state.present.document.definition.edges).toEqual([]);
    expect(state.present.document.fixtures[0].mocks).toEqual([]);
    expect(referenceIssues(state.present.document, specs).map(issue => issue.message)).toEqual(['来源已删除', '来源已删除']);
    expect(designerHistory(state, { type: 'undo' }).present).toEqual(initial);
  });
  it('rewrites exact nested output references without editing code, literals or similarly named fields', () => {
    const document = graph();
    document.definition.nodes[1].input_mapping.other = '$.nodes.a.output.result_other';
    const renamed = rewriteOutputReferences(document, 'a', 'result', 'renamed');
    expect(renamed.definition.nodes[1].input_mapping.payload[0]).toBe('$.nodes.a.output.renamed.value');
    expect(renamed.definition.edges[0].condition.path).toBe('$.nodes.a.output.renamed.value');
    expect(renamed.fixtures[0].assertions[0].path).toBe('$.nodes.a.output.renamed.value');
    expect(renamed.definition.nodes[1].config.code).toBe('$.nodes.a.output.result.value');
    expect(renamed.definition.nodes[1].input_mapping.other).toBe('$.nodes.a.output.result_other');
  });
  it('rechecks disconnected, removed and type-changed source fields', () => {
    const document = graph();
    expect(referenceIssues(document, specs)).toEqual([]);
    document.definition.nodes[0].config.output_schema.properties.result.properties.value.type = 'number';
    expect(referenceIssues(document, specs)[0].message).toBe('来源字段类型不匹配');
    delete document.definition.nodes[0].config.output_schema.properties.result.properties.value;
    expect(referenceIssues(document, specs)[0].message).toBe('来源字段不存在');
    document.definition.edges = [];
    expect(referenceIssues(document, specs)[0].message).toBe('来源已不在上游');
  });
  it('inserts on an edge without moving its branch condition and rejects cycles', () => {
    const document = graph();
    const result = insertOnEdge(document, 'ab', { id: 'middle', type: 'data.python' });
    expect(result.definition.edges[0]).toMatchObject({ source: 'a', target: 'middle', condition: document.definition.edges[0].condition });
    expect(result.definition.edges[1]).toMatchObject({ source: 'middle', target: 'b' });
    expect(validConnection(result.definition, { source: 'b', target: 'a' })).toBe(false);
    expect(validConnection(document.definition, { source: 'a', target: 'b', sourceHandle: null, targetHandle: null })).toBe(false);
  });
  it('bounds history and clears redo after a new edit', () => {
    let state = { present: { n: 0 }, past: [], future: [] };
    for (let i = 0; i < 60; i += 1) state = designerHistory(state, { type: 'edit', update: value => ({ n: value.n + 1 }) });
    expect(state.past).toHaveLength(50);
    state = designerHistory(state, { type: 'undo' });
    expect(state.future[0].n).toBe(60);
    state = designerHistory(state, { type: 'edit', update: value => ({ n: value.n + 2 }) });
    expect(state.future).toEqual([]);
  });
});

import { renameMappedField } from './v2Designer';
it('renames nested input variables without touching expressions or code', () => {
  const value = { rows: [{ old: '$.inputs.number' }, { old: '100' }], literal: 'old' };
  expect(renameMappedField(value, 'rows.*.old', 'rows.*.number')).toEqual({ rows: [{ number: '$.inputs.number' }, { number: '100' }], literal: 'old' });
  expect(value.rows[0]).toEqual({ old: '$.inputs.number' });
});
