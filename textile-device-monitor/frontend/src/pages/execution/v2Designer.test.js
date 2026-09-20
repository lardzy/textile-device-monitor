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
