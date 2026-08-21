import { createClientUuid } from './clientId';

export const WORKFLOW_SCHEMA_VERSION = '1.0';

export const createWorkflowNode = (nodeTypeOrDefinition, position = { x: 0, y: 0 }) => {
  const nodeType = typeof nodeTypeOrDefinition === 'string'
    ? nodeTypeOrDefinition
    : nodeTypeOrDefinition?.type;
  const catalog = typeof nodeTypeOrDefinition === 'string'
    ? {
      type: nodeType,
      label: nodeType,
      tone: 'default',
    }
    : {
      ...nodeTypeOrDefinition,
      label: nodeTypeOrDefinition?.label || nodeTypeOrDefinition?.name || nodeType,
      group: nodeTypeOrDefinition?.group || nodeTypeOrDefinition?.category || '其他',
      configSchema: nodeTypeOrDefinition?.configSchema
        || nodeTypeOrDefinition?.config_schema
        || {},
      tone: nodeTypeOrDefinition?.tone || 'default',
    };
  return {
    id: `${nodeType}-${createClientUuid()}`,
    type: 'executionNode',
    position,
    data: {
      nodeType,
      typeVersion: Number(catalog.version || catalog.type_version || 1),
      label: catalog.label,
      config: {},
      inputMapping: {},
      category: catalog.group,
      description: catalog.description,
      configSchema: catalog.configSchema || catalog.config_schema || {},
      tone: catalog.tone,
    },
  };
};

export const createDefaultDefinition = (metadata = {}) => {
  const start = createWorkflowNode({
    type: 'core.start',
    version: 1,
    name: '开始',
    category: '基础',
    description: '流程入口',
    tone: 'blue',
  }, { x: 120, y: 220 });
  const end = createWorkflowNode({
    type: 'core.end',
    version: 1,
    name: '结束',
    category: '基础',
    description: '流程出口',
    tone: 'green',
  }, { x: 520, y: 220 });
  return {
    schema_version: WORKFLOW_SCHEMA_VERSION,
    metadata,
    category: metadata.category || null,
    input_schema: {
      type: 'object',
      required: ['inspection_number'],
      properties: {
        inspection_number: {
          type: 'string',
          title: '检验编号',
        },
      },
    },
    global_schema: {
      type: 'object',
      properties: {},
    },
    root_slots: [],
    credential_slots: [],
    nodes: [start, end],
    edges: [{
      id: `edge-${createClientUuid()}`,
      source: start.id,
      target: end.id,
      join_policy: 'all',
    }],
    viewport: { x: 0, y: 0, zoom: 1 },
  };
};

const normalizeNode = node => ({
  ...node,
  type: 'executionNode',
  position: node.position
    || node.ui?.position
    || (node.ui && Number.isFinite(node.ui.x) && Number.isFinite(node.ui.y)
      ? { x: node.ui.x, y: node.ui.y }
      : { x: 0, y: 0 }),
  data: {
    nodeType: node.data?.nodeType || node.node_type || node.type || 'result.aggregate',
    typeVersion: Number(node.data?.typeVersion || node.type_version || 1),
    label: node.data?.label || node.name || node.id,
    config: node.data?.config || node.config || {},
    inputMapping: node.data?.inputMapping || node.input_mapping || {},
    disabled: node.data?.disabled === true || node.disabled === true,
    status: node.data?.status || node.status,
    message: node.data?.message || node.message,
    category: node.data?.category,
    description: node.data?.description,
    configSchema: node.data?.configSchema || {},
    tone: node.data?.tone,
  },
});

export const isWorkflowReleaseV2Document = value => Boolean(
  value
  && typeof value === 'object'
  && (
    value.format === 'textile-workflow-release'
    || value.format_version === '2.0'
    || value.definition?.schema_version === '2.0'
  )
);

export const normalizeWorkflowDefinition = (value, metadata = {}) => {
  if (isWorkflowReleaseV2Document(value)) {
    const error = new Error('Workflow Release v2 必须在独立 Release 管理页中处理');
    error.code = 'workflow_release_v2_requires_release_manager';
    throw error;
  }
  const raw = value?.definition || value?.draft_definition || value || {};
  const fallback = createDefaultDefinition(metadata);
  return {
    ...fallback,
    ...raw,
    metadata: { ...fallback.metadata, ...(raw.metadata || {}) },
    input_schema: raw.input_schema || fallback.input_schema,
    global_schema: raw.global_schema || fallback.global_schema,
    root_slots: raw.root_slots || [],
    credential_slots: raw.credential_slots || [],
    nodes: Array.isArray(raw.nodes) ? raw.nodes.map(normalizeNode) : fallback.nodes,
    edges: Array.isArray(raw.edges) ? raw.edges : [],
    viewport: raw.viewport || fallback.viewport,
  };
};

export const definitionFromCanvas = (definition, nodes, edges, viewport) => ({
  ...definition,
  schema_version: WORKFLOW_SCHEMA_VERSION,
  nodes: nodes.map(node => ({
    id: node.id,
    type: node.data.nodeType,
    type_version: Number(node.data.typeVersion || 1),
    name: node.data.label,
    config: node.data.config || {},
    input_mapping: node.data.inputMapping || {},
    disabled: node.data.disabled === true,
    ui: { x: node.position.x, y: node.position.y },
  })),
  edges: edges.map(edge => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.sourceHandle,
    targetHandle: edge.targetHandle,
    condition: edge.data?.condition || edge.condition || null,
    join_policy: edge.data?.joinPolicy || edge.join_policy || 'all',
    label: edge.label,
  })),
  viewport,
});

const containsUnsafeAbsolutePath = (value, key = '') => {
  if (typeof value === 'string') {
    if (!/(path|file|directory|root)/i.test(key)) {
      return false;
    }
    return /^(?:[a-zA-Z]:[\\/]|\\\\|\/|smb:\/\/|file:\/\/)/.test(value);
  }
  if (Array.isArray(value)) {
    return value.some(item => containsUnsafeAbsolutePath(item, key));
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).some(([childKey, child]) =>
      containsUnsafeAbsolutePath(child, childKey),
    );
  }
  return false;
};

export const validateWorkflowDefinition = (definition, registeredNodeTypes = null) => {
  const errors = [];
  const nodes = definition?.nodes || [];
  const edges = definition?.edges || [];
  const ids = new Set(nodes.map(node => node.id));
  const activeNodes = nodes.filter(node => !(
    node.disabled === true || node.data?.disabled === true
  ));
  const activeIds = new Set(activeNodes.map(node => node.id));
  const activeEdges = edges.filter(edge => (
    activeIds.has(edge.source) && activeIds.has(edge.target)
  ));
  const nodeTypes = activeNodes.map(node => node.data?.nodeType || node.type);
  const registeredNodeKeys = Array.isArray(registeredNodeTypes)
    ? new Set(registeredNodeTypes.map(item => `${item.type}@${Number(item.version || 1)}`))
    : null;

  if (definition?.schema_version !== WORKFLOW_SCHEMA_VERSION) {
    errors.push(`仅支持工作流结构版本 ${WORKFLOW_SCHEMA_VERSION}`);
  }
  if (new Set(nodes.map(node => node.id)).size !== nodes.length) {
    errors.push('节点 ID 不能重复');
  }
  if (nodeTypes.filter(type => type === 'core.start').length !== 1) {
    errors.push('流程必须且只能包含一个开始节点');
  }
  if (!nodeTypes.includes('core.end')) {
    errors.push('流程至少需要一个结束节点');
  }
  nodes.forEach((node) => {
    const type = node.data?.nodeType || node.type;
    const version = Number(node.data?.typeVersion || node.type_version || 1);
    if (registeredNodeKeys && !registeredNodeKeys.has(`${type}@${version}`)) {
      errors.push(`节点“${node.data?.label || node.name || node.id}”类型或版本未在服务端注册`);
    }
    if (containsUnsafeAbsolutePath(node.data?.config || node.config || {})) {
      errors.push(`节点“${node.data?.label || node.name || node.id}”包含裸绝对路径`);
    }
  });
  edges.forEach((edge) => {
    if (!ids.has(edge.source) || !ids.has(edge.target)) {
      errors.push('连线引用了不存在的节点');
    }
    if (edge.source === edge.target) {
      errors.push('节点不能连接到自身');
    }
  });

  const outgoing = new Map(activeNodes.map(node => [node.id, []]));
  const indegree = new Map(activeNodes.map(node => [node.id, 0]));
  activeEdges.forEach((edge) => {
    if (outgoing.has(edge.source) && indegree.has(edge.target)) {
      outgoing.get(edge.source).push(edge.target);
      indegree.set(edge.target, indegree.get(edge.target) + 1);
    }
  });
  const queue = [...indegree.entries()].filter(([, degree]) => degree === 0).map(([id]) => id);
  let visited = 0;
  while (queue.length) {
    const id = queue.shift();
    visited += 1;
    outgoing.get(id).forEach((target) => {
      indegree.set(target, indegree.get(target) - 1);
      if (indegree.get(target) === 0) {
        queue.push(target);
      }
    });
  }
  if (visited !== activeNodes.length) {
    errors.push('首版流程必须是无环图，检测到循环依赖');
  }
  return errors;
};

export const mergeNodeRunStatuses = (nodes, nodeRuns = []) => {
  const byId = new Map(
    nodeRuns.map(run => [run.node_id || run.nodeId, run]),
  );
  return nodes.map((node) => {
    const run = byId.get(node.id);
    return run
      ? {
        ...node,
        data: {
          ...node.data,
          status: run.status,
          message: run.message || run.error_message || run.error?.message,
          durationMs: run.duration_ms,
        },
      }
      : node;
  });
};

const ACTIVE_EXECUTION_NODE_STATUSES = new Set([
  'ready',
  'running',
  'waiting_human',
  'waiting_external',
]);

const nodeRunId = node => node?.node_id || node?.nodeId || node?.id;

const uniqueNodeIds = nodeRuns => [...new Set(
  nodeRuns
    .map(nodeRunId)
    .filter(Boolean)
    .map(String),
)];

export const resolveExecutionFocusNodeIds = (
  nodeRuns = [],
  definitionNodes = [],
  runStatus = '',
) => {
  const activeNodeIds = uniqueNodeIds(
    nodeRuns.filter(node => ACTIVE_EXECUTION_NODE_STATUSES.has(node?.status)),
  );
  if (activeNodeIds.length) {
    return activeNodeIds;
  }

  const failedNodeIds = uniqueNodeIds(
    nodeRuns.filter(node => node?.status === 'failed'),
  );
  if (failedNodeIds.length) {
    return failedNodeIds;
  }

  if (!['completed', 'succeeded'].includes(runStatus)) {
    return [];
  }

  const endNodeIds = new Set(
    definitionNodes
      .filter(node => (
        node?.data?.nodeType || node?.node_type || node?.type
      ) === 'core.end')
      .map(node => String(node.id)),
  );
  return uniqueNodeIds(nodeRuns.filter(node => (
    ['completed', 'succeeded'].includes(node?.status)
    && (
      node?.node_type === 'core.end'
      || endNodeIds.has(String(nodeRunId(node)))
    )
  )));
};
