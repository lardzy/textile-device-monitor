import { describe, expect, it } from 'vitest';
import {
  createDefaultDefinition,
  createWorkflowNode,
  definitionFromCanvas,
  normalizeWorkflowDefinition,
  validateWorkflowDefinition,
} from './executionWorkflow';

describe('execution workflow definition', () => {
  it('round-trips canvas nodes through the versioned JSON contract', () => {
    const definition = createDefaultDefinition({ name: '测试流程' });
    const serialized = definitionFromCanvas(
      definition,
      definition.nodes,
      definition.edges,
      { x: 12, y: 20, zoom: 0.8 },
    );
    const normalized = normalizeWorkflowDefinition(serialized);

    expect(serialized.schema_version).toBe('1.0');
    expect(serialized.nodes[0].type).toBe('core.start');
    expect(normalized.nodes[0].type).toBe('executionNode');
    expect(normalized.nodes[0].data.nodeType).toBe('core.start');
    expect(normalized.viewport).toEqual({ x: 12, y: 20, zoom: 0.8 });
  });

  it('uses the server registry version when creating a new canvas node', () => {
    const node = createWorkflowNode({
      type: 'custom.server_node',
      version: 7,
      name: '服务端节点',
      category: '扩展',
      description: '由后端注册表提供',
      config_schema: {
        type: 'object',
        properties: { mode: { type: 'string' } },
      },
    });
    const serialized = definitionFromCanvas(
      createDefaultDefinition(),
      [node],
      [],
      { x: 0, y: 0, zoom: 1 },
    );

    expect(node.data).toMatchObject({
      nodeType: 'custom.server_node',
      typeVersion: 7,
      label: '服务端节点',
      category: '扩展',
    });
    expect(serialized.nodes[0]).toMatchObject({
      type: 'custom.server_node',
      type_version: 7,
      name: '服务端节点',
    });
  });

  it('rejects cycles and unsafe absolute paths before server publishing', () => {
    const definition = createDefaultDefinition();
    const writeNode = createWorkflowNode('workbook.write_cells', { x: 320, y: 100 });
    writeNode.data.config = { source_path: '/Volumes/private/source.xlsx' };
    definition.nodes.push(writeNode);
    definition.edges = [
      { id: 'a', source: definition.nodes[0].id, target: writeNode.id },
      { id: 'b', source: writeNode.id, target: definition.nodes[0].id },
    ];

    const errors = validateWorkflowDefinition(definition);
    expect(errors.some(error => error.includes('绝对路径'))).toBe(true);
    expect(errors.some(error => error.includes('循环依赖'))).toBe(true);
  });

  it('requires exactly one start node and at least one end node', () => {
    const definition = createDefaultDefinition();
    definition.nodes = definition.nodes.filter(node => node.data.nodeType !== 'core.end');
    definition.nodes.push(createWorkflowNode('core.start'));

    expect(validateWorkflowDefinition(definition)).toEqual(expect.arrayContaining([
      '流程必须且只能包含一个开始节点',
      '流程至少需要一个结束节点',
    ]));
  });

  it('serializes branch conditions and join policy without nullable backend values', () => {
    const definition = createDefaultDefinition();
    const [edge] = definition.edges;
    const serialized = definitionFromCanvas(
      definition,
      definition.nodes,
      [{
        ...edge,
        data: {
          condition: {
            path: '$.globals.needs_review',
            operator: 'eq',
            value: true,
          },
          joinPolicy: 'any',
        },
      }],
      definition.viewport,
    );

    expect(serialized.edges[0]).toMatchObject({
      condition: {
        path: '$.globals.needs_review',
        operator: 'eq',
        value: true,
      },
      join_policy: 'any',
    });
    expect(serialized.nodes.every(node => Number.isInteger(node.type_version))).toBe(true);
  });

  it('保留停放节点的草稿配置，但从可执行图校验中排除其相邻连线', () => {
    const definition = createDefaultDefinition();
    const parked = createWorkflowNode('result.aggregate', { x: 320, y: 100 });
    parked.data.disabled = true;
    definition.nodes.push(parked);
    definition.edges.push(
      { id: 'parked-in', source: definition.nodes[0].id, target: parked.id },
      { id: 'parked-cycle', source: parked.id, target: definition.nodes[0].id },
    );

    const serialized = definitionFromCanvas(
      definition,
      definition.nodes,
      definition.edges,
      definition.viewport,
    );
    const normalized = normalizeWorkflowDefinition(serialized);

    expect(validateWorkflowDefinition(serialized)).not.toContain(
      '首版流程必须是无环图，检测到循环依赖',
    );
    expect(serialized.nodes.find(node => node.id === parked.id).disabled).toBe(true);
    expect(normalized.nodes.find(node => node.id === parked.id).data.disabled).toBe(true);
  });
});
