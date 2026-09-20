export const specKey = node => `${node.type}@${node.type_version}`;
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
