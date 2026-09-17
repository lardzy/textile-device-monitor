import { MemoryRouter } from 'react-router-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, expect, it } from 'vitest';
import { server } from '../../../tests/testServer';
import { resetExecutionCsrfToken } from '../../api/executionClient';
import WorkflowReplacementPanel from './WorkflowReplacementPanel';

const pending = () => ({
  status: 'pending',
  source: { workflow_id: 'old', slug: 'old-flow', name: '旧流程', draft_revision: 2, version_number: 1, is_enabled: true, archived_at: null },
  target: { workflow_id: 'new', slug: 'old-flow-v2', name: '新流程', draft_revision: 3, version_number: 2, is_enabled: false, archived_at: null },
  last_action: null,
});

beforeEach(() => {
  resetExecutionCsrfToken();
  server.use(http.get('/api/execution/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'replacement-csrf' })));
});

it('switches with both revisions and versions, then reverts with the activation receipt', async () => {
  let state = pending();
  const calls = [];
  server.use(
    http.get('/api/execution/v2/workflows/new/replacement', () => HttpResponse.json(state)),
    http.post('/api/execution/v2/workflows/new/replacement/:action', async ({ request, params }) => {
      expect(request.headers.get('X-CSRF-Token')).toBe('replacement-csrf');
      const payload = await request.json();
      calls.push([params.action, payload]);
      const active = params.action === 'activate';
      state = {
        ...state, status: active ? 'active' : 'reverted',
        source: { ...state.source, draft_revision: state.source.draft_revision + 1, archived_at: active ? '2026-09-05T00:00:00Z' : null, is_enabled: !active },
        target: { ...state.target, draft_revision: state.target.draft_revision + 1, is_enabled: active },
        last_action: { id: active ? 17 : 18, action: `workflow_replacement.${params.action}` },
      };
      return HttpResponse.json(state);
    }),
  );
  const user = userEvent.setup();
  render(<MemoryRouter><WorkflowReplacementPanel workflowId="new" /></MemoryRouter>);
  expect(await screen.findByText('已发布，等待接替')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '归档旧流程并启用新流程' })).toBeDisabled();
  await user.type(screen.getByLabelText('接替或回切原因'), '三条选择流程验收完成');
  await user.click(screen.getByRole('button', { name: '归档旧流程并启用新流程' }));
  expect(await screen.findByText('新流程已接替')).toBeInTheDocument();
  await user.type(screen.getByLabelText('接替或回切原因'), '恢复旧入口');
  await user.click(screen.getByRole('button', { name: '回切旧流程' }));
  expect(await screen.findByText('已回切旧流程')).toBeInTheDocument();
  expect(calls).toEqual([
    ['activate', { expected_source_revision: 2, expected_target_revision: 3, expected_source_version: 1, expected_target_version: 2, reason: '三条选择流程验收完成' }],
    ['revert', { expected_source_revision: 3, expected_target_revision: 4, expected_source_version: 1, expected_target_version: 2, reason: '恢复旧入口', activation_audit_id: 17 }],
  ]);
});

it('refreshes after a revision conflict and requires a new click with current state', async () => {
  let state = pending();
  let attempts = 0;
  server.use(
    http.get('/api/execution/v2/workflows/new/replacement', () => HttpResponse.json(state)),
    http.post('/api/execution/v2/workflows/new/replacement/activate', () => {
      attempts += 1;
      state = { ...state, target: { ...state.target, version_number: 4, draft_revision: 5 } };
      return HttpResponse.json({ code: 'replacement_revision_changed', message: '双方版本已变化' }, { status: 409 });
    }),
  );
  const user = userEvent.setup();
  render(<MemoryRouter><WorkflowReplacementPanel workflowId="new" /></MemoryRouter>);
  await screen.findByText('已发布，等待接替');
  await user.type(screen.getByLabelText('接替或回切原因'), '接替');
  await user.click(screen.getByRole('button', { name: '归档旧流程并启用新流程' }));
  await waitFor(() => expect(screen.getByText(/old-flow-v2 · v4 · rev 5/)).toBeInTheDocument());
  expect(attempts).toBe(1);
  expect(screen.queryByText('新流程已接替')).not.toBeInTheDocument();
});
