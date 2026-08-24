import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../../tests/testServer';
import { resetExecutionCsrfToken } from '../../api/executionClient';
import { ExecutionAuthProvider } from './ExecutionAuthContext';
import ExecutionWorkflowReleaseManager from './ExecutionWorkflowReleaseManager';

const releaseDocument = {
  format: 'textile-workflow-release',
  format_version: '2.0',
  release: {
    slug: 'v2-readonly-file-query-smoke',
    release_version: 1,
    name: '只读文件查询',
  },
  definition: {
    schema_version: '2.0',
    nodes: [],
    edges: [],
  },
};

const renderManager = (initialPath = '/execution/admin/releases') => render(
  <MemoryRouter
    initialEntries={[initialPath]}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <ExecutionAuthProvider>
      <Routes>
        <Route path="/execution/admin/releases" element={<ExecutionWorkflowReleaseManager />} />
        <Route path="/execution/admin/releases/:releaseId" element={<ExecutionWorkflowReleaseManager />} />
        <Route path="/execution/admin" element={<div>v1 流程管理</div>} />
      </Routes>
    </ExecutionAuthProvider>
  </MemoryRouter>,
);

describe('ExecutionWorkflowReleaseManager', () => {
  beforeEach(() => {
    resetExecutionCsrfToken();
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'admin-1',
          username: 'admin',
          role: 'admin',
          permissions: ['workflow.design', 'workflow.publish'],
        },
      })),
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'release-token' })),
      http.get('/api/execution/v1/files/roots', () => HttpResponse.json({
        items: [{ id: 'root-readonly', name: '只读检测目录' }],
      })),
      http.get('/api/execution/v2/workflow-releases', () => HttpResponse.json({
        items: [], has_more: false, next_cursor: null,
      })),
      http.get('/api/execution/v2/packs', () => HttpResponse.json({ items: [] })),
      http.get('/api/execution/v2/renderer-capabilities', () => HttpResponse.json({
        items: [], registry_revision: 'registry-1', rollout_profile: 'p1_readonly',
      })),
      http.get('/api/execution/v2/monitoring', () => HttpResponse.json({
        registry_revision: 'registry-1',
        rollout_profile: 'p1_readonly',
        shadow_mismatch: { count: 0 },
        node_capability_unavailable: { unavailable_node_count: 0 },
      })),
    );
  });

  it('completes content preflight, staged apply, binding and publish without using the v1 importer', async () => {
    const calls = [];
    server.use(
      http.post('/api/execution/v2/workflow-releases/preflight', async ({ request }) => {
        expect(request.headers.get('X-CSRF-Token')).toBe('release-token');
        const body = await request.json();
        calls.push(['content-preflight', body]);
        return HttpResponse.json({
          report: {
            content_valid: true,
            publish_ready: false,
            release_digest: 'a'.repeat(64),
            registry_revision: 'registry-1',
            required_bindings: [{ slot: 'inspection_files', access: 'read' }],
            issues: [],
            preflight_token: 'content-token',
          },
        });
      }),
      http.post('/api/execution/v2/workflow-releases/apply', async ({ request }) => {
        const body = await request.json();
        calls.push(['apply', body]);
        return HttpResponse.json({
          release: {
            id: 'release-1',
            status: 'staged',
            required_bindings: [{ slot: 'inspection_files', access: 'read' }],
          },
        });
      }),
      http.get('/api/execution/v2/workflow-releases/release-1', () => {
        calls.push(['get-release', null]);
        return HttpResponse.json({
          release: {
            id: 'release-1',
            status: 'staged',
            binding_revision: 0,
            required_bindings: [{ slot: 'inspection_files', access: 'read' }],
          },
        });
      }),
      http.put('/api/execution/v2/workflow-releases/release-1/deployment-binding', async ({ request }) => {
        const body = await request.json();
        calls.push(['binding', body]);
        return HttpResponse.json({
          deployment_binding: {
            revision: 1,
            root_bindings: body.root_bindings,
          },
        });
      }),
      http.post('/api/execution/v2/workflow-releases/release-1/preflight', () =>
        HttpResponse.json({
          report: {
            content_valid: true,
            publish_ready: true,
            issues: [],
            preflight_token: 'publish-token',
          },
        })),
      http.post('/api/execution/v2/workflow-releases/release-1/publish', async ({ request }) => {
        const body = await request.json();
        calls.push(['publish', body]);
        return HttpResponse.json({
          release: {
            id: 'release-1',
            status: 'published',
            workflow_id: 'workflow-1',
            local_version: 1,
          },
        });
      }),
    );

    const user = userEvent.setup();
    renderManager();
    const editor = await screen.findByRole('textbox', { name: 'Workflow Release JSON' });
    fireEvent.change(editor, { target: { value: JSON.stringify(releaseDocument) } });
    await user.click(screen.getByRole('button', { name: /内容预检$/ }));

    expect(await screen.findByText('预检未发现问题')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Apply 为 staged$/ }));
    await waitFor(() => {
      expect(calls).toContainEqual(['get-release', null]);
      expect(screen.getByText('Staged Release')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('combobox', { name: /inspection_files/ }));
    await user.click(await screen.findByText('只读检测目录'));
    await user.click(screen.getByRole('button', { name: '保存绑定' }));
    await user.click(screen.getByRole('button', { name: '重新预检' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /发布 Release$/ })).toBeEnabled();
    });
    await user.click(screen.getByRole('button', { name: /发布 Release$/ }));
    expect(await screen.findByText('published')).toBeInTheDocument();

    expect(calls).toEqual(expect.arrayContaining([
      ['content-preflight', { document: releaseDocument }],
      ['apply', { preflight_token: 'content-token' }],
      ['binding', {
        environment: 'default',
        expected_revision: 0,
        root_bindings: [{
          slot: 'inspection_files',
          storage_root_id: 'root-readonly',
          role: 'read',
        }],
      }],
      ['publish', { preflight_token: 'publish-token' }],
    ]));
  });

  it('shows v1 migration output as preview-only data', async () => {
    server.use(
      http.get('/api/execution/v1/workflows', () => HttpResponse.json({
        items: [{ id: 'workflow-v1', name: '旧流程', management_mode: 'draft_v1' }],
      })),
      http.post('/api/execution/v2/migrations/v1/preview', async ({ request }) => {
        expect(await request.json()).toEqual({
          workflow_id: 'workflow-v1',
          source: 'published',
          target_profile: 'compat_v1',
        });
        return HttpResponse.json({
          candidate: releaseDocument,
          content_valid: true,
          migration_status: 'compatibility_preview',
          native_node_count: 0,
          compatibility_node_count: 0,
          blockers: [],
          diff: [{ path: '$.definition.schema_version', before: '1.0', after: '2.0' }],
        });
      }),
    );

    const user = userEvent.setup();
    renderManager();
    await user.click(await screen.findByRole('button', { name: /v1 候选预览$/ }));
    const dialog = await screen.findByRole('dialog', { name: 'v1 → v2 候选迁移预览' });
    await user.click(within(dialog).getByRole('combobox', { name: 'v1 草稿流程' }));
    await user.click(await screen.findByText('旧流程'));
    await user.click(within(dialog).getByRole('button', { name: '生成候选与差异' }));

    expect(await within(dialog).findByText(/schema_version/)).toBeInTheDocument();
    expect(within(dialog).getByText(/不提供本轮激活入口/)).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: /发布|激活/ })).not.toBeInTheDocument();
  });

  it('hydrates a published release and its immutable binding without the v1 canvas shape', async () => {
    const exported = [];
    const rollbacks = [];
    const releaseResponse = {
      id: 'release-published',
      status: 'published',
      workflow_id: 'workflow-published',
      active_local_version: 1,
      local_versions: [1, 2],
      document: releaseDocument,
      required_bindings: [{ slot_id: 'inspection_files', access: 'read' }],
      deployment_binding: {
        revision: 3,
        bindings: {
          root_slots: {
            inspection_files: {
              root_id: 'root-readonly',
              storage_root_id: 'storage-row-1',
              revision: 4,
            },
          },
        },
      },
    };
    server.use(
      http.get('/api/execution/v2/workflow-releases/release-published', () =>
        HttpResponse.json(releaseResponse)),
      http.get('/api/execution/v2/workflows/workflow-published/versions/2/export', () => {
        exported.push(true);
        return HttpResponse.json(releaseDocument);
      }),
      http.post('/api/execution/v2/workflows/workflow-published/rollback', async ({ request }) => {
        rollbacks.push(await request.json());
        return HttpResponse.json({ activation: { to_version: 1 } });
      }),
    );
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:release-test'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    const user = userEvent.setup();
    renderManager('/execution/admin/releases/release-published');

    const editor = await screen.findByRole('textbox', { name: 'Workflow Release JSON' });
    expect(editor).toHaveAttribute('readonly');
    expect(JSON.parse(editor.value)).toEqual(releaseDocument);
    expect(screen.getByText('v2-readonly-file-query-smoke')).toBeInTheDocument();
    expect(screen.getByText('最新本地版本')).toBeInTheDocument();
    expect(screen.getByText('当前激活版本')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /导出 portable Release$/ }));
    await waitFor(() => expect(exported).toEqual([true]));

    await user.type(screen.getByRole('spinbutton', { name: '目标本地版本' }), '1');
    await user.type(screen.getByRole('textbox', { name: '回滚原因' }), '回退只读基线');
    await user.click(screen.getByRole('button', { name: /回滚 active pointer$/ }));
    await waitFor(() => {
      expect(rollbacks).toEqual([{
        target_local_version: 1,
        reason: '回退只读基线',
      }]);
    });
    expect(screen.queryByTestId('workflow-canvas')).not.toBeInTheDocument();
    anchorClick.mockRestore();
  });
});
