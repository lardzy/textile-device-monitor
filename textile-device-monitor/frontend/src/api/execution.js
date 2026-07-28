import executionClient, {
  getExecutionCsrfToken,
  resetExecutionCsrfToken,
} from './executionClient';

const listPayload = (payload, keys) => {
  if (Array.isArray(payload)) {
    return payload;
  }
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) {
      return payload[key];
    }
  }
  return [];
};

export const executionAuthApi = {
  csrf: getExecutionCsrfToken,
  me: () => executionClient.get('/auth/me'),
  login: async credentials => {
    resetExecutionCsrfToken();
    const result = await executionClient.post('/auth/login', credentials, {
      skipCsrf: true,
    });
    resetExecutionCsrfToken();
    await getExecutionCsrfToken();
    return result;
  },
  logout: async () => {
    try {
      return await executionClient.post('/auth/logout');
    } finally {
      resetExecutionCsrfToken();
    }
  },
};

export const getExecutionCategories = async () =>
  listPayload(await executionClient.get('/categories'), ['items', 'categories']);

export const getExecutionUsers = async () =>
  listPayload(await executionClient.get('/users'), ['items', 'users']);

export const createExecutionUser = payload =>
  executionClient.post('/users', payload);

export const updateExecutionUser = (userId, payload) =>
  executionClient.patch(`/users/${userId}`, payload);

export const getExecutionCredentials = async () =>
  listPayload(await executionClient.get('/credentials'), ['items', 'credentials']);

export const upsertExecutionCredential = (systemKey, payload) =>
  executionClient.put(`/credentials/${systemKey}`, payload);

export const getExecutionWorkflows = async (params = {}) =>
  listPayload(await executionClient.get('/workflows', { params }), ['items', 'workflows']);

export const getExecutionCatalogRecommendations = async (
  {
    inspectionNumber = '',
    preferredCategories = [],
  } = {},
  { signal } = {},
) => {
  const params = new URLSearchParams();
  if (inspectionNumber.trim()) {
    params.set('inspection_number', inspectionNumber.trim());
  }
  preferredCategories.forEach((category) => {
    params.append('preferred_categories', category);
  });
  return listPayload(
    await executionClient.get('/catalog/recommendations', { params, signal }),
    ['items', 'recommendations'],
  );
};

export const getExecutionWorkflow = workflowId =>
  executionClient.get(`/workflows/${workflowId}`);

export const getExecutionNodeTypes = async () =>
  listPayload(await executionClient.get('/node-types'), ['items', 'node_types']);

export const createExecutionWorkflow = payload =>
  executionClient.post('/workflows', payload);

export const saveWorkflowDraft = (workflowId, payload) =>
  executionClient.put(`/workflows/${workflowId}/draft`, payload);

export const validateWorkflow = (workflowId, definition) =>
  executionClient.post(`/workflows/${workflowId}/validate`, definition);

export const publishWorkflow = (workflowId, payload) =>
  executionClient.post(`/workflows/${workflowId}/publish`, payload);

export const testWorkflow = (workflowId, payload) =>
  executionClient.post(`/workflows/${workflowId}/test`, payload);

export const getWorkflowVersions = async workflowId =>
  listPayload(
    await executionClient.get(`/workflows/${workflowId}/versions`),
    ['items', 'versions'],
  );

export const importWorkflow = (workflowId, document, expectedRevision) =>
  executionClient.post('/workflows/import', {
    document,
    overwrite_workflow_id: workflowId,
    expected_revision: expectedRevision,
  });

export const exportWorkflow = workflowId =>
  executionClient.get(`/workflows/${workflowId}/export`);

export const createExecutionRun = payload =>
  executionClient.post('/runs', payload);

export const getExecutionRuns = async (params = {}) =>
  listPayload(await executionClient.get('/runs', { params }), ['items', 'runs']);

export const getExecutionRunsPage = async (params = {}) => {
  const payload = await executionClient.get('/runs', { params });
  const items = listPayload(payload, ['items', 'runs']);
  return {
    items,
    total: Number(payload?.total ?? items.length),
    offset: Number(payload?.offset ?? params.offset ?? 0),
    limit: Number(payload?.limit ?? params.limit ?? items.length),
  };
};

export const getExecutionRun = runId =>
  executionClient.get(`/runs/${runId}`);

export const getExecutionRunEventHistory = (runId, params = {}) =>
  executionClient.get(`/runs/${runId}/event-history`, { params });

export const pauseExecutionRun = runId =>
  executionClient.post(`/runs/${runId}/pause`);

export const resumeExecutionRun = runId =>
  executionClient.post(`/runs/${runId}/resume`);

export const cancelExecutionRun = runId =>
  executionClient.post(`/runs/${runId}/cancel`);

export const retryExecutionNode = (runId, nodeId) =>
  executionClient.post(`/runs/${runId}/nodes/${nodeId}/retry`, {});

export const getExecutionRunMutations = async runId =>
  listPayload(
    await executionClient.get(`/runs/${runId}/mutations`),
    ['items', 'mutations'],
  );

export const getExecutionRunMutation = (runId, mutationId) =>
  executionClient.get(
    `/runs/${runId}/mutations/${encodeURIComponent(mutationId)}`,
  );

export const preflightExecutionMutation = (runId, payload) =>
  executionClient.post(`/runs/${runId}/mutations/preflight`, payload);

export const copyExecutionMutation = (runId, mutationId, payload = {}) =>
  executionClient.post(
    `/runs/${runId}/mutations/${encodeURIComponent(mutationId)}/copy`,
    payload,
  );

export const writeExecutionMutation = (runId, mutationId, payload) =>
  executionClient.post(
    `/runs/${runId}/mutations/${encodeURIComponent(mutationId)}/write`,
    payload,
  );

export const verifyExecutionMutation = (runId, mutationId, payload) =>
  executionClient.post(
    `/runs/${runId}/mutations/${encodeURIComponent(mutationId)}/verify`,
    payload,
  );

export const publishExecutionMutation = (runId, mutationId, payload) =>
  executionClient.post(
    `/runs/${runId}/mutations/${encodeURIComponent(mutationId)}/publish`,
    payload,
  );

export const getExecutionPublishReceipt = receiptId =>
  executionClient.get(`/publish-receipts/${encodeURIComponent(receiptId)}`);

const executionApiBase = () => (
  import.meta.env.VITE_EXECUTION_API_URL || '/api/execution/v1'
).replace(/\/+$/, '');

export const executionArtifactPreviewUrl = artifactId =>
  `${executionApiBase()}/artifacts/${encodeURIComponent(artifactId)}/preview`;

export const getHumanTasks = async (params = {}) =>
  listPayload(
    await executionClient.get('/human-tasks', { params }),
    ['items', 'human_tasks', 'tasks'],
  );

export const getHumanTask = taskId =>
  executionClient.get(`/human-tasks/${taskId}`);

export const claimHumanTask = (taskId, revision) =>
  executionClient.post(`/human-tasks/${taskId}/claim`, { revision });

export const saveHumanTaskDraft = (taskId, revision, values) =>
  executionClient.put(`/human-tasks/${taskId}/draft`, {
    revision,
    data: values,
  });

export const submitHumanTask = (taskId, revision, values) =>
  executionClient.post(`/human-tasks/${taskId}/submit`, {
    revision,
    data: values,
  });

export const rejectHumanTask = (taskId, revision, reason) =>
  executionClient.post(`/human-tasks/${taskId}/reject`, { revision, reason });

export const searchExecutionFiles = params =>
  executionClient.get('/files/search', { params });

export const refreshExecutionFiles = rootId =>
  executionClient.post('/files/refresh', { root_id: rootId });

export const executionEventsUrl = runId => {
  const base = executionApiBase();
  return `${base}/runs/${encodeURIComponent(runId)}/events`;
};

export { executionClient };
