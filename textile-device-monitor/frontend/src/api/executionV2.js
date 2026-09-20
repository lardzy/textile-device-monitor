import { executionV2Client } from './executionClient';

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

export const getExecutionV2NodeSpecs = async (params = {}) =>
  listPayload(
    await executionV2Client.get('/node-specs', { params }),
    ['items', 'node_specs'],
  );

export const getExecutionV2NodeSpec = (type, version) =>
  executionV2Client.get(
    `/node-specs/${encodeURIComponent(type)}/${encodeURIComponent(version)}`,
  );

export const getExecutionV2Packs = async () =>
  listPayload(await executionV2Client.get('/packs'), ['items', 'packs']);

export const getExecutionV2Assets = async () =>
  listPayload(await executionV2Client.get('/assets'), ['items', 'assets']);

export const getExecutionTemplatesV2 = async params =>
  listPayload(await executionV2Client.get('/templates', { params }), ['items']);

export const getExecutionV2RendererCapabilities = () =>
  executionV2Client.get('/renderer-capabilities');

export const getExecutionV2Monitoring = (windowHours = 24) =>
  executionV2Client.get('/monitoring', { params: { window_hours: windowHours } });

export const getWorkflowReleasesV2 = (params = {}) =>
  executionV2Client.get('/workflow-releases', { params });

export const preflightWorkflowReleaseV2 = document =>
  executionV2Client.post('/workflow-releases/preflight', { document });

export const applyWorkflowReleaseV2 = preflightToken =>
  executionV2Client.post('/workflow-releases/apply', {
    preflight_token: preflightToken,
  });

export const getWorkflowReleaseV2 = releaseId =>
  executionV2Client.get(`/workflow-releases/${encodeURIComponent(releaseId)}`);

export const updateWorkflowReleaseBindingV2 = (releaseId, payload) =>
  executionV2Client.put(
    `/workflow-releases/${encodeURIComponent(releaseId)}/deployment-binding`,
    payload,
  );

export const preflightStagedWorkflowReleaseV2 = (releaseId, payload = {}) =>
  executionV2Client.post(
    `/workflow-releases/${encodeURIComponent(releaseId)}/preflight`,
    payload,
  );

export const publishWorkflowReleaseV2 = (releaseId, preflightToken) =>
  executionV2Client.post(
    `/workflow-releases/${encodeURIComponent(releaseId)}/publish`,
    { preflight_token: preflightToken },
  );

export const exportWorkflowReleaseV2 = (workflowId, localVersion) =>
  executionV2Client.get(
    `/workflows/${encodeURIComponent(workflowId)}/versions/${encodeURIComponent(localVersion)}/export`,
  );

export const rollbackWorkflowReleaseV2 = (workflowId, payload) =>
  executionV2Client.post(
    `/workflows/${encodeURIComponent(workflowId)}/rollback`,
    payload,
  );

export const previewWorkflowV1Migration = (
  workflowId,
  source = 'published',
  targetProfile = 'compat_v1',
  targetSlug,
) =>
  executionV2Client.post('/migrations/v1/preview', {
    workflow_id: workflowId,
    source,
    target_profile: targetProfile,
    ...(targetSlug ? { target_slug: targetSlug } : {}),
  });

export const getWorkflowReplacementV2 = workflowId =>
  executionV2Client.get(`/workflows/${encodeURIComponent(workflowId)}/replacement`);

export const activateWorkflowReplacementV2 = (workflowId, payload) =>
  executionV2Client.post(`/workflows/${encodeURIComponent(workflowId)}/replacement/activate`, payload);

export const revertWorkflowReplacementV2 = (workflowId, payload) =>
  executionV2Client.post(`/workflows/${encodeURIComponent(workflowId)}/replacement/revert`, payload);

export { executionV2Client };

export const getWorkflowDesignerCatalogV2 = () => executionV2Client.get('/designer/catalog');
export const compileWorkflowDesignerV2 = document => executionV2Client.post('/designer/compile', { document });
export const getExecutionCompatibilityAudit = () => executionV2Client.get('/compatibility/audit');
