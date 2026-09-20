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
      http.put('/api/execution/v2/workflow-releases/:id/deployment-binding', async ({ request }) => {
        const body = await request.json();
        expect(body.root_bindings).toEqual([]);
        return HttpResponse.json({ deployment_binding: { id: 'empty-binding', revision: 1, root_bindings: [] } });
      }),
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

  it('selects the current personal credential revision and creates the first binding automatically', async () => {
    let saved;
    const document = { ...releaseDocument, resources: { root_slots: [], credential_slots: [{ slot_id: 'account', name: '检务账号', connector_id: 'legacy_fibrecheck', required: true }] } };
    server.use(
      http.get('/api/execution/v1/credentials', () => HttpResponse.json({ items: [{ id: 'my-account', system_key: 'legacy_inspection', account_name: '本人账号', is_active: true, configured: true, revision: 3 }] })),
      http.get('/api/execution/v2/workflow-releases/account-release', () => HttpResponse.json({ id: 'account-release', status: 'staged', document })),
      http.put('/api/execution/v2/workflow-releases/account-release/deployment-binding', async ({ request }) => {
        saved = await request.json();
        return HttpResponse.json({ deployment_binding: { id: 'binding', revision: 1, bindings: saved.bindings } });
      }),
      http.post('/api/execution/v2/workflow-releases/account-release/preflight', () => HttpResponse.json({ content_valid: true, publish_ready: false, issues: [], required_bindings: [{ kind: 'credential_slot', slot_id: 'account', required: true }] })),
    );
    renderManager('/execution/admin/releases/account-release');
    await screen.findByRole('combobox', { name: '检务账号' });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '检务账号' }).closest('.ant-select')).toHaveTextContent('本人账号'));
    await userEvent.click(screen.getByRole('button', { name: /检查并发布/ }));
    await waitFor(() => expect(saved?.bindings.credential_slots).toEqual({ account: { credential_id: 'my-account', revision: 3 } }));
    expect(saved.expected_revision).toBe(0);
    expect(screen.queryByRole('combobox', { name: /account.*read/ })).not.toBeInTheDocument();
  });

  it('publishes with the current project rule revision and existing root binding in one action', async () => {
    const calls = [];
    const document = { ...releaseDocument, resources: {
      root_slots: [{ slot_id: 'source', access: 'read', required: true }],
      rule_slots: [{ slot_id: 'record_match', name: '再生纤面积法', required: true }],
    } };
    server.use(
      http.get('/api/execution/v1/files/roots', () => HttpResponse.json({ items: [
        { id: 'root-1', root_id: 'records', name: '再生纤目录', binding_revision: 3 },
      ] })),
      http.get('/api/execution/v1/project-rules', () => HttpResponse.json({ items: [
        { rule_key: 'fiber.area', display_name: '再生纤面积法', revision: 2, enabled: true },
      ] })),
      http.get('/api/execution/v2/workflow-releases/p3', () => HttpResponse.json({ release: {
        id: 'p3', status: 'staged', portable_document: document,
        deployment_binding: { environment: 'default', revision: 2, bindings: {
          root_slots: { source: { root_id: 'records', revision: 3 } },
          rule_slots: { record_match: { rule_key: 'fiber.area', revision: 1 } },
        } },
      } })),
      http.put('/api/execution/v2/workflow-releases/p3/deployment-binding', async ({ request }) => {
        const body = await request.json();
        calls.push('save');
        expect(body.expected_revision).toBe(2);
        expect(body.bindings.root_slots).toEqual({ source: { root_id: 'records', revision: 3 } });
        expect(body.bindings.rule_slots).toEqual({ record_match: { rule_key: 'fiber.area', revision: 2 } });
        return HttpResponse.json({ deployment_binding: { revision: 3, bindings: body.bindings } });
      }),
      http.post('/api/execution/v2/workflow-releases/p3/preflight', () => {
        calls.push('preflight');
        return HttpResponse.json({ report: { publish_ready: true, preflight_token: 'p3-ready' } });
      }),
      http.post('/api/execution/v2/workflow-releases/p3/publish', () => {
        calls.push('publish');
        return HttpResponse.json({ release: { id: 'p3', status: 'published', local_version: 1 } });
      }),
    );
    renderManager('/execution/admin/releases/p3');
    await screen.findByRole('combobox', { name: '再生纤面积法' });
    await waitFor(() => expect(screen.getByRole('combobox', { name: '再生纤面积法' })
      .closest('.ant-select').querySelector('.ant-select-selection-item')).toHaveTextContent('再生纤面积法'));
    await userEvent.click(screen.getByRole('button', { name: /检查并发布/ }));
    await waitFor(() => expect(calls).toEqual(['save', 'preflight', 'publish']));
  });

  it('导入自动检查，发布一次完成保存绑定、检查和发布', async () => {
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
      http.post('/api/execution/v2/workflow-releases/release-1/preflight', () => {
        calls.push(['publish-preflight', null]);
        return HttpResponse.json({
          report: {
            content_valid: true,
            publish_ready: true,
            issues: [],
            preflight_token: 'publish-token',
          },
        });
      }),
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
    await user.click(screen.getByRole('button', { name: /导入并检查$/ }));

    await waitFor(() => {
      expect(calls).toContainEqual(['get-release', null]);
      expect(screen.getByText('版本信息')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('combobox', { name: /inspection_files/ }));
    await user.click(await screen.findByText('只读检测目录'));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /检查并发布$/ })).toBeEnabled();
    });
    await user.click(screen.getByRole('button', { name: /检查并发布$/ }));
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
    expect(calls.filter(([name]) => name !== 'get-release').map(([name]) => name)).toEqual([
      'content-preflight', 'apply', 'binding', 'publish-preflight', 'publish',
    ]);
  });

  it('发布检查失败只显示问题，不调用发布接口', async () => {
    const calls = [];
    server.use(
      http.get('/api/execution/v2/workflow-releases/invalid', () => HttpResponse.json({ id: 'invalid', status: 'staged' })),
      http.post('/api/execution/v2/workflow-releases/invalid/preflight', () => {
        calls.push('check');
        return HttpResponse.json({ content_valid: true, publish_ready: false, issues: [{ severity: 'error', code: 'worker_unavailable', message: '没有匹配的 Worker' }] });
      }),
      http.post('/api/execution/v2/workflow-releases/invalid/publish', () => {
        calls.push('publish');
        return HttpResponse.json({});
      }),
    );
    const user = userEvent.setup();
    renderManager('/execution/admin/releases/invalid');
    await user.click(await screen.findByRole('button', { name: /检查并发布/ }));
    expect(await screen.findByText('没有匹配的 Worker')).toBeInTheDocument();
    expect(calls).toEqual(['check']);
  });

  it('绑定版本冲突时保留选择，停止后续发布请求', async () => {
    const calls = [];
    server.use(
      http.get('/api/execution/v2/workflow-releases/conflict', () => HttpResponse.json({
        id: 'conflict', status: 'staged', binding_revision: 2,
        required_bindings: [{ slot: 'inspection_files', access: 'read' }],
      })),
      http.put('/api/execution/v2/workflow-releases/conflict/deployment-binding', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json({ detail: { code: 'binding_revision_conflict', message: '绑定已被修改，请刷新' } }, { status: 409 });
      }),
      http.post('/api/execution/v2/workflow-releases/conflict/preflight', () => {
        calls.push('unexpected-check');
        return HttpResponse.json({});
      }),
    );
    const user = userEvent.setup();
    renderManager('/execution/admin/releases/conflict');
    await user.click(await screen.findByRole('combobox', { name: /inspection_files/ }));
    await user.click(await screen.findByText('只读检测目录'));
    await user.click(screen.getByRole('button', { name: /检查并发布/ }));
    expect(await screen.findByText('绑定已被修改，请刷新')).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0].expected_revision).toBe(2);
    expect(screen.getByRole('combobox', { name: /inspection_files/ }).closest('.ant-select').querySelector('.ant-select-selection-item')).toHaveTextContent('只读检测目录');
  });

  it('shows v1 migration output as preview-only data', async () => {
    server.use(
      http.get('/api/execution/v1/workflows', () => HttpResponse.json({
        items: [{ id: 'workflow-v1', slug: 'old-workflow', name: '旧流程', management_mode: 'draft_v1' }],
      })),
      http.post('/api/execution/v2/migrations/v1/preview', async ({ request }) => {
        expect(await request.json()).toEqual({
          workflow_id: 'workflow-v1',
          source: 'published',
          target_profile: 'native_p4',
          target_slug: 'old-workflow-v2',
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
    expect(within(dialog).getByText(/候选不会修改来源流程/)).toBeInTheDocument();
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
    await user.click(await screen.findByText('流程 JSON 与检查明细'));

    const editor = await screen.findByRole('textbox', { name: 'Workflow Release JSON' });
    expect(editor).toHaveAttribute('readonly');
    expect(JSON.parse(editor.value)).toEqual(releaseDocument);
    expect(screen.getByText('v2-readonly-file-query-smoke')).toBeInTheDocument();
    expect(screen.getByText('最新本地版本')).toBeInTheDocument();
    expect(screen.getByText('当前激活版本')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /导出流程 JSON$/ }));
    await waitFor(() => expect(exported).toEqual([true]));

    await user.type(screen.getByRole('spinbutton', { name: '目标本地版本' }), '1');
    await user.type(screen.getByRole('textbox', { name: '回滚原因' }), '回退只读基线');
    await user.click(screen.getByRole('button', { name: /回滚到所选版本$/ }));
    await waitFor(() => {
      expect(rollbacks).toEqual([{
        target_local_version: 1,
        reason: '回退只读基线',
      }]);
    });
    expect(screen.queryByTestId('workflow-canvas')).not.toBeInTheDocument();
    anchorClick.mockRestore();
  });

  it('freezes local replacement evidence in publish preflight after a staged page reload', async () => {
    const replacementSource = { workflow_id: 'workflow-old', version_id: 'version-old', version_number: 2, draft_revision: 3, definition_checksum: 'a'.repeat(64), contract_checksum: 'b'.repeat(64) };
    const calls = [];
    server.use(
      http.get('/api/execution/v2/workflow-releases/replacement-staged', () => HttpResponse.json({
        id: 'replacement-staged', status: 'staged', migration_source: replacementSource,
        document: { ...releaseDocument, migration: { source_digest: replacementSource.definition_checksum } },
      })),
      http.post('/api/execution/v2/workflow-releases/replacement-staged/preflight', async ({ request }) => {
        calls.push(await request.json());
        return HttpResponse.json({ content_valid: true, publish_ready: true, preflight_token: 'replacement-preflight', replacement_source: replacementSource, issues: [] });
      }),
    );
    const user = userEvent.setup();
    renderManager('/execution/admin/releases/replacement-staged');
    expect(await screen.findByText('首次发布保持停用')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '仅检查' }));
    await waitFor(() => expect(calls).toEqual([{ replacement_source: replacementSource }]));
    expect(screen.getByRole('button', { name: /检查并发布$/ })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '归档旧流程并启用新流程' })).not.toBeInTheDocument();
  });
});
