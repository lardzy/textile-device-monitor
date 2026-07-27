import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { server } from '../../tests/testServer';
import {
  copyExecutionMutation,
  createExecutionRun,
  executionAuthApi,
  getExecutionPublishReceipt,
  getExecutionRunMutation,
  getExecutionRunMutations,
  preflightExecutionMutation,
  publishExecutionMutation,
  saveWorkflowDraft,
  verifyExecutionMutation,
  writeExecutionMutation,
} from './execution';
import { resetExecutionCsrfToken } from './executionClient';

describe('execution API security contract', () => {
  beforeEach(() => {
    resetExecutionCsrfToken();
  });

  it('does not require CSRF before login and protects subsequent mutations', async () => {
    const calls = [];
    server.use(
      http.post('/api/execution/v1/auth/login', async ({ request }) => {
        calls.push('login');
        expect(request.headers.get('X-CSRF-Token')).toBeNull();
        expect(await request.json()).toEqual({
          username: 'operator',
          password: 'correct-password',
        });
        return HttpResponse.json({
          user: { id: 'u-1', username: 'operator' },
          csrf_token: 'login-token',
        });
      }),
      http.get('/api/execution/v1/auth/csrf', () => {
        calls.push('csrf');
        return HttpResponse.json({ csrf_token: 'session-token' });
      }),
      http.post('/api/execution/v1/runs', ({ request }) => {
        calls.push('run');
        expect(request.headers.get('X-CSRF-Token')).toBe('session-token');
        return HttpResponse.json({ run: { id: 'run-1' } });
      }),
    );

    await executionAuthApi.login({
      username: 'operator',
      password: 'correct-password',
    });
    await createExecutionRun({
      workflow_id: 'wf-1',
      inspection_number: '26X1',
      input_data: { inspection_number: '26X1' },
      global_data: {},
      idempotency_key: 'mutation-123',
    });

    expect(calls).toEqual(['login', 'csrf', 'run']);
  });

  it('keeps the structured 409 conflict payload for the designer', async () => {
    server.use(
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'session-token' })),
      http.put('/api/execution/v1/workflows/wf-1/draft', () =>
        HttpResponse.json({
          code: 'workflow_revision_conflict',
          message: '草稿已被其他页面更新',
          details: { current_revision: 8 },
          request_id: 'request-1',
        }, { status: 409 })),
    );

    await expect(saveWorkflowDraft('wf-1', {
      revision: 7,
      definition: {},
    })).rejects.toMatchObject({
      status: 409,
      code: 'workflow_revision_conflict',
      details: { current_revision: 8 },
      requestId: 'request-1',
    });
  });

  it('uses the staged file-mutation and receipt endpoints without changing payloads', async () => {
    const received = [];
    const record = name => async ({ request }) => {
      received.push({
        name,
        body: request.method === 'GET' ? null : await request.json(),
        csrf: request.headers.get('X-CSRF-Token'),
      });
      return HttpResponse.json({ mutation: { mutation_id: 'mutation-1', status: name } });
    };
    server.use(
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'mutation-token' })),
      http.get('/api/execution/v1/runs/run-1/mutations', () =>
        HttpResponse.json({ items: [{ mutation_id: 'mutation-1' }] })),
      http.get('/api/execution/v1/runs/run-1/mutations/mutation-1', () =>
        HttpResponse.json({ mutation: { mutation_id: 'mutation-1', status: 'planned' } })),
      http.post('/api/execution/v1/runs/run-1/mutations/preflight', record('preflight')),
      http.post('/api/execution/v1/runs/run-1/mutations/mutation-1/copy', record('copy')),
      http.post('/api/execution/v1/runs/run-1/mutations/mutation-1/write', record('write')),
      http.post('/api/execution/v1/runs/run-1/mutations/mutation-1/verify', record('verify')),
      http.post('/api/execution/v1/runs/run-1/mutations/mutation-1/publish', record('publish')),
      http.get('/api/execution/v1/publish-receipts/receipt-1', () =>
        HttpResponse.json({ receipt: { id: 'receipt-1' } })),
    );

    expect(await getExecutionRunMutations('run-1')).toEqual([
      { mutation_id: 'mutation-1' },
    ]);
    expect(await getExecutionRunMutation('run-1', 'mutation-1')).toMatchObject({
      mutation: { mutation_id: 'mutation-1' },
    });

    const source = { root_id: 'inspection_records', relative_path: 'source.xlsx' };
    const target = { root_id: 'execution_publish', relative_path: 'result.xlsx' };
    const writes = [{ sheet: '记录', cell: 'B2', value: 12 }];
    const approvalContext = {
      approved: true,
      mutation_id: 'mutation-1',
      working_copy: {
        root_id: 'execution_staging',
        relative_path: 'run-1/source.xlsx',
        content_sha256: 'a'.repeat(64),
      },
      target,
      change_plan_checksum: 'b'.repeat(64),
    };

    await preflightExecutionMutation('run-1', {
      mutation_id: 'mutation-1',
      source,
      node_id: 'copy-node',
    });
    await copyExecutionMutation('run-1', 'mutation-1', { node_id: 'copy-node' });
    await writeExecutionMutation('run-1', 'mutation-1', {
      writes,
      node_id: 'write-node',
    });
    await verifyExecutionMutation('run-1', 'mutation-1', {
      target,
      writes,
      node_id: 'verify-node',
    });
    await publishExecutionMutation('run-1', 'mutation-1', {
      target,
      approval_context: approvalContext,
      node_id: 'publish-node',
    });
    expect(await getExecutionPublishReceipt('receipt-1')).toEqual({
      receipt: { id: 'receipt-1' },
    });

    expect(received).toEqual([
      {
        name: 'preflight',
        body: { mutation_id: 'mutation-1', source, node_id: 'copy-node' },
        csrf: 'mutation-token',
      },
      {
        name: 'copy',
        body: { node_id: 'copy-node' },
        csrf: 'mutation-token',
      },
      {
        name: 'write',
        body: { writes, node_id: 'write-node' },
        csrf: 'mutation-token',
      },
      {
        name: 'verify',
        body: { target, writes, node_id: 'verify-node' },
        csrf: 'mutation-token',
      },
      {
        name: 'publish',
        body: {
          target,
          approval_context: approvalContext,
          node_id: 'publish-node',
        },
        csrf: 'mutation-token',
      },
    ]);
  });
});
