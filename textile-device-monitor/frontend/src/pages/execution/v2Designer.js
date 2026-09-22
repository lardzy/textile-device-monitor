export const specKey = node => `${node.type}@${node.type_version}`;
export const upstreamNodeIds = (definition, nodeId) => {
  const result = new Set();
  const pending = [nodeId];
  while (pending.length) {
    const current = pending.pop();
    (definition?.edges || []).filter(edge => edge.target === current).forEach(edge => {
      if (edge.source !== nodeId && !result.has(edge.source)) {
        result.add(edge.source);
        pending.push(edge.source);
      }
    });
  }
  return result;
};
// Suggestions only: actual schema validation remains with the release compiler.
export const compatibleTypes = (source = {}, target = {}) => {
  const types = schema => Array.isArray(schema?.type) ? schema.type : schema?.type ? [schema.type] : [];
  const from = types(source), to = types(target);
  return !from.length || !to.length || from.some(type => to.includes(type) || (type === 'integer' && to.includes('number')));
};
export const schemaDefaults = schema => Object.fromEntries(
  Object.entries(schema?.properties || {}).flatMap(([key, value]) => (
    Object.hasOwn(value, 'const') ? [[key, value.const]]
      : Object.hasOwn(value, 'default') ? [[key, structuredClone(value.default)]] : []
  )),
);
export const graphNodes = (document, specs) => document.definition.nodes.map((node, index) => {
  const spec = specs.find(item => specKey(item) === specKey(node));
  return { id: node.id, type: 'executionNode', position: { x: node.ui?.x ?? index * 240, y: node.ui?.y ?? 120 },
    data: { label: node.name || spec?.name, nodeType: node.type, category: spec?.category,
      description: spec?.description, ports: spec?.ports,
      tone: ({ human: 'orange', external_side_effect: 'purple' })[spec?.execution?.kind] || 'default' } };
});
export const graphEdges = document => document.definition.edges.map(edge => ({
  ...edge, sourceHandle: edge.source_handle, targetHandle: edge.target_handle,
  label: edge.label || (edge.condition === 'default' ? '其他' : edge.condition ? '条件' : ''),
}));
export const portableEdge = connection => ({
  id: `edge-${crypto.randomUUID()}`, source: connection.source, target: connection.target, join_policy: 'all',
  ...(connection.sourceHandle ? { source_handle: connection.sourceHandle } : {}),
  ...(connection.targetHandle ? { target_handle: connection.targetHandle } : {}),
});
export const parseMapping = value => {
  if (!value.trim()) return undefined;
  try { return JSON.parse(value); } catch { return value; }
};
export const effectiveSchemas = (node, spec, connectors = [], definition = {}) => {
  if (!node || !spec) return {};
  const contracts = connectors.flatMap(connector => [...(connector.queries || []), ...(connector.operations || [])]);
  const ref = node.config?.query_ref || node.config?.operation_ref;
  const contract = contracts.find(item => item.query_ref === ref || item.operation_ref === ref);
  const result = {};
  ['input', 'output'].forEach((direction) => {
    const binding = spec.schema_bindings?.[direction];
    result[direction] = binding?.source === 'workflow_input_schema' ? definition.input_schema
      : binding?.source === 'workflow_output_schema' ? definition.output_schema
        : binding?.source === 'node_config'
      ? node.config?.[binding.pointer?.replace(/^\//, '')]
      : ['query_spec', 'operation_spec'].includes(binding?.source) ? contract?.[`${direction}_schema`]
        : spec[`${direction}_schema`];
  });
  return result;
};

export const isReference = value => typeof value === 'string' && value.startsWith('$.');

export function collectReferences(document) {
  const result = [];
  const visit = (value, path, owner) => {
    if (isReference(value)) result.push({ expression: value, path, ...owner });
    else if (Array.isArray(value)) value.forEach((item, index) => visit(item, [...path, index], owner));
    else if (value && typeof value === 'object') Object.entries(value).forEach(([key, item]) => visit(item, [...path, key], owner));
  };
  (document.definition?.nodes || []).forEach((node, index) => visit(node.input_mapping, ['definition', 'nodes', index, 'input_mapping'], { nodeId: node.id, label: node.name }));
  (document.definition?.edges || []).forEach((edge, index) => {
    if (isReference(edge.condition?.path)) result.push({ expression: edge.condition.path, path: ['definition', 'edges', index, 'condition', 'path'], edgeId: edge.id, nodeId: edge.source, allowSelf: true, label: edge.label || '连接条件' });
  });
  (document.fixtures || []).forEach((fixture, index) => {
    (fixture.assertions || []).forEach((assertion, position) => {
      if (isReference(assertion.path)) result.push({ expression: assertion.path, path: ['fixtures', index, 'assertions', position, 'path'], fixtureId: fixture.fixture_id || index, label: '样例断言' });
    });
  });
  return result;
}

export function referenceIssues(document, specs = [], connectors = []) {
  if (!document) return [];
  const definition = document.definition;
  return collectReferences(document).flatMap(reference => {
    const source = [...definition.nodes].sort((a, b) => b.id.length - a.id.length).find(node => ['output', 'status'].some(kind => reference.expression === `$.nodes.${node.id}.${kind}` || reference.expression.startsWith(`$.nodes.${node.id}.${kind}.`)));
    const match = source ? reference.expression.slice(`$.nodes.${source.id}.`.length).match(/^(output|status)(?:\.(.*))?$/) : reference.expression.match(/^\$\.nodes\.(.+?)\.(output|status)(?:\.(.*))?$/);
    if (!match) {
      const inputPath = reference.expression.startsWith('$.inputs.') ? reference.expression.slice(9).split('.') : null;
      if (!inputPath) return [];
      let schema = definition.input_schema;
      for (const key of inputPath) {
        if (schema?.type === 'array' && /^\d+$/.test(key)) schema = schema.items;
        else if (schema?.properties?.[key]) schema = schema.properties[key];
        else return schema?.additionalProperties === false ? [{ ...reference, message: '流程输入字段不存在' }] : [];
      }
      return [];
    }
    const [id, kind, suffix] = source ? [source.id, match[1], match[2]] : match.slice(1);
    let message;
    if (!source) message = '来源已删除';
    else if (reference.fixtureId === undefined && !(reference.allowSelf && reference.nodeId === id) && !upstreamNodeIds(definition, reference.nodeId).has(id)) message = '来源已不在上游';
    else if (kind === 'output') {
      let schema = effectiveSchemas(source, specs.find(spec => specKey(spec) === specKey(source)), connectors, definition).output;
      for (const key of suffix ? suffix.split('.') : []) {
        if (!schema) break;
        if (schema.type === 'array' && /^\d+$/.test(key)) schema = schema.items;
        else if (schema.properties?.[key]) schema = schema.properties[key];
        else { if (schema.additionalProperties === false) message = '来源字段不存在'; schema = undefined; break; }
      }
      if (!message && reference.fixtureId === undefined && !reference.edgeId && schema) {
        const target = definition.nodes.find(node => node.id === reference.nodeId);
        let expected = effectiveSchemas(target, specs.find(spec => target && specKey(spec) === specKey(target)), connectors, definition).input;
        for (const key of reference.path.slice(4)) expected = expected?.type === 'array' ? expected.items : expected?.properties?.[key];
        if (expected && !compatibleTypes(schema, expected)) message = '来源字段类型不匹配';
      }
    }
    return message ? [{ ...reference, message, sourceNodeId: id }] : [];
  });
}

export function deleteSelection(document, nodeIds, edgeIds = []) {
  const result = structuredClone(document);
  const removed = new Set(nodeIds);
  result.definition.nodes = result.definition.nodes.filter(node => !removed.has(node.id));
  result.definition.edges = result.definition.edges.filter(edge => !removed.has(edge.source) && !removed.has(edge.target) && !edgeIds.includes(edge.id));
  (result.fixtures || []).forEach(fixture => {
    if (Array.isArray(fixture.mocks)) fixture.mocks = fixture.mocks.filter(mock => !removed.has(mock.node_id));
  });
  return result;
}

export function rewriteReferences(document, root, oldKey, newKey) {
  const result = structuredClone(document);
  const before = `${root}.${oldKey}`.split('.'), after = `${root}.${newKey}`.split('.');
  collectReferences(result).forEach(reference => {
    const parts = reference.expression.split('.');
    if (!before.every((key, index) => key === '*' ? /^\d+$/.test(parts[index] || '') : key === parts[index])) return;
    const rewritten = [...after.map((key, index) => key === '*' ? parts[index] : key), ...parts.slice(before.length)].join('.');
    let parent = result;
    reference.path.slice(0, -1).forEach(key => { parent = parent[key]; });
    parent[reference.path.at(-1)] = rewritten;
  });
  return result;
}
export const rewriteOutputReferences = (document, nodeId, oldKey, newKey) => rewriteReferences(document, `$.nodes.${nodeId}.output`, oldKey, newKey);

export function validConnection(definition, connection, replacingId) {
  const { source, target, sourceHandle, targetHandle } = connection;
  if (!source || !target || source === target) return false;
  const from = definition.nodes.find(node => node.id === source);
  const to = definition.nodes.find(node => node.id === target);
  if (!from || !to || from.type === 'core.end' || to.type === 'core.start') return false;
  const edges = definition.edges.filter(edge => edge.id !== replacingId);
  if (edges.some(edge => edge.source === source && edge.target === target && (edge.source_handle || '') === (sourceHandle || '') && (edge.target_handle || '') === (targetHandle || ''))) return false;
  return !upstreamNodeIds({ ...definition, edges }, source).has(target);
}

export function insertOnEdge(document, edgeId, node) {
  const result = structuredClone(document);
  const edge = result.definition.edges.find(item => item.id === edgeId);
  result.definition.nodes.push(node);
  if (edge) {
    const target = edge.target, targetHandle = edge.target_handle, join = edge.join_policy;
    edge.target = node.id; delete edge.target_handle; edge.join_policy = 'all';
    result.definition.edges.push({ id: `edge-${crypto.randomUUID()}`, source: node.id, target, join_policy: join || 'all', ...(targetHandle ? { target_handle: targetHandle } : {}) });
  }
  return result;
}

export function variableTree(schema, prefix, label, depth = 0) {
  const children = depth < 8 ? Object.entries(schema?.properties || {}).map(([key, value]) => variableTree(value, `${prefix}.${key}`, value.title || key, depth + 1)) : [];
  if (schema?.type === 'array' && schema.items && depth < 8) children.push(variableTree(schema.items, `${prefix}.0`, '第 1 项', depth + 1));
  return { title: label, value: prefix, key: prefix, children, schema };
}

export function designerHistory(state, action) {
  if (action.type === 'reset') return { present: action.value, past: [], future: [], group: null };
  if (action.type === 'undo' && state.past.length) return { present: state.past.at(-1), past: state.past.slice(0, -1), future: [state.present, ...state.future], group: null };
  if (action.type === 'redo' && state.future.length) return { present: state.future[0], past: [...state.past, state.present].slice(-50), future: state.future.slice(1), group: null };
  if (action.type === 'select') return JSON.stringify(state.present?.selection) === JSON.stringify(action.selection) ? state : { ...state, present: { ...state.present, selection: action.selection } };
  if (action.type === 'break') return { ...state, group: null };
  if (action.type !== 'edit' || !state.present) return state;
  const next = action.update(structuredClone(state.present));
  if (JSON.stringify(next) === JSON.stringify(state.present)) return state;
  const coalesce = action.group && action.group === state.group && action.at - state.at < 1000;
  return { present: next, past: coalesce ? state.past : [...state.past, state.present].slice(-50), future: [], group: action.group, at: action.at };
}
