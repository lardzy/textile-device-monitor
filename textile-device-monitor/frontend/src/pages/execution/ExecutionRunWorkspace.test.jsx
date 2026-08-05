import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { server } from '../../../tests/testServer';
import { ExecutionAuthProvider } from './ExecutionAuthContext';
import ExecutionRunWorkspace from './ExecutionRunWorkspace';
import useExecutionEvents from './useExecutionEvents';

vi.mock('./useExecutionEvents', () => ({
  default: vi.fn(),
}));

const completedRun = {
  id: 'run-1',
  workflow_id: 'wf-1',
  workflow_name: '原始记录发现',
  inspection_number: '26X910095-1',
  status: 'completed',
  mode: 'normal',
  input_data: {
    inspection_number: '26X910095-1',
    remark: '复核后提交',
  },
  global_data: {
    batch_name: '第一批',
  },
  output_data: {},
  nodes: [],
  human_tasks: [],
  artifacts: [],
  events: [],
  definition: {
    schema_version: '1.0',
    metadata: { name: '原始记录发现' },
    input_schema: {
      type: 'object',
      properties: {
        inspection_number: { type: 'string', title: '检验编号' },
        remark: { type: 'string', title: '备注' },
      },
      required: ['inspection_number'],
    },
    global_schema: {
      type: 'object',
      properties: {
        batch_name: { type: 'string', title: '批次名称' },
      },
    },
    nodes: [],
    edges: [],
  },
};

describe('ExecutionRunWorkspace', () => {
  it('确认取消后关闭弹窗、仅提交一次并刷新终态', async () => {
    let runStatus = 'running';
    let cancelRequests = 0;
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.read', 'workflow.run'],
        },
      })),
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'csrf-cancel' })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        status: runStatus,
      })),
      http.post('/api/execution/v1/runs/run-1/cancel', () => {
        cancelRequests += 1;
        runStatus = 'cancelled';
        return HttpResponse.json({
          duplicate: false,
          run: { ...completedRun, status: runStatus },
        });
      }),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /^取\s*消$/ }));
    const confirmDialog = await screen.findByRole('dialog');
    expect(within(confirmDialog).getAllByText('取消本次执行？')).not.toHaveLength(0);
    await user.click(within(confirmDialog).getByRole('button', { name: '确认取消' }));

    await waitFor(() => expect(cancelRequests).toBe(1));
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(await screen.findByText('已取消')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^取\s*消$/ })).not.toBeInTheDocument();
  });

  it('返回时关闭取消弹窗且不提交请求', async () => {
    let cancelRequests = 0;
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.read', 'workflow.run'],
        },
      })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        status: 'running',
      })),
      http.post('/api/execution/v1/runs/run-1/cancel', () => {
        cancelRequests += 1;
        return HttpResponse.json({});
      }),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /^取\s*消$/ }));
    const confirmDialog = await screen.findByRole('dialog');
    await user.click(within(confirmDialog).getByRole('button', { name: /^返\s*回$/ }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(cancelRequests).toBe(0);
    expect(screen.getByRole('button', { name: /^取\s*消$/ })).toBeInTheDocument();
  });

  it('只读展示创建运行时已提交的输入和全局变量', async () => {
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.read', 'workflow.run', 'human_task.handle'],
        },
      })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json(completedRun)),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole('textbox', { name: '检验编号' })).toHaveValue('26X910095-1');
    expect(screen.getByRole('textbox', { name: '检验编号' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '备注' })).toHaveValue('复核后提交');
    expect(screen.getByRole('textbox', { name: '备注' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '批次名称' })).toHaveValue('第一批');
    expect(screen.getByRole('textbox', { name: '批次名称' })).toBeDisabled();
    expect(screen.getByText('已提交的全局变量')).toBeInTheDocument();
  });

  it('流程结束后按文件保留结果、备注和主单标识', async () => {
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.read', 'workflow.run', 'file.read'],
        },
      })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        output_data: {
          selected_files: [{
            id: 'candidate-result-1',
            name: '262039607-面积法.xls',
            read_status: 'succeeded',
            result: {
              parts: [{
                name: '领口',
                components: [
                  { name: '棉', content: 88 },
                  { name: '莱赛尔', content: 12 },
                ],
              }],
              remarks: ['已与复核结果确认'],
              images: [],
            },
          }],
          primary_file_id: 'candidate-result-1',
        },
      })),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('262039607-面积法.xls')).toBeInTheDocument();
    expect(screen.getByText('领口')).toBeInTheDocument();
    expect(screen.getByText('88%')).toBeInTheDocument();
    expect(screen.getByText('莱赛尔')).toBeInTheDocument();
    expect(screen.getByText('已与复核结果确认')).toBeInTheDocument();
    expect(screen.getByText('主单')).toBeInTheDocument();
    expect(screen.queryByText(/selected_files/)).not.toBeInTheDocument();
  });

  it('展示旧系统上传预检清单并用完整编号确认本地批准', async () => {
    let operationStatus = 'prepared';
    let approvalBody = null;
    const operation = () => ({
      id: 'external-operation-1',
      run_id: 'run-1',
      node_run_id: 'node-run-upload',
      status: operationStatus,
      payload_checksum: 'a'.repeat(64),
      request_summary: {
        target_sample_number: '260187115-1',
        source_inspection_number: '260187115',
        inspector: '辜惠珊',
        business_fields: {
          fiber_category: '棉再生纤',
          inspection_method: '定量',
          inspection_item: '棉再生纤定量-根数法',
          inspection_copies: 1,
        },
        files: [{
          id: 'source-file-1',
          filename: '260187115-根数法.xls',
          relative_path: '7月/260187115-根数法.xls',
          size_bytes: 235520,
          content_sha256: 'b'.repeat(64),
          is_primary: true,
        }],
      },
      approval: {
        approved_at: operationStatus === 'approved'
          ? '2026-07-29T08:00:00Z'
          : null,
      },
    });
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.read', 'workflow.run'],
        },
      })),
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'csrf-external' })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        status: 'waiting_external',
        nodes: [{
          id: 'node-run-upload',
          node_id: 'upload',
          node_type: 'external.legacy_regenerated_fiber_count_upload',
          name: '旧系统上传-再生纤-根数法',
          status: 'waiting_external',
        }],
        definition: {
          ...completedRun.definition,
          nodes: [{
            id: 'upload',
            type: 'external.legacy_regenerated_fiber_count_upload',
            type_version: 1,
            name: '旧系统上传-再生纤-根数法',
            config: {},
            input_mapping: {},
            ui: { x: 0, y: 0 },
          }],
        },
      })),
      http.get('/api/execution/v1/runs/run-1/external-operations', () =>
        HttpResponse.json({ items: [operation()] })),
      http.post(
        '/api/execution/v1/external-operations/external-operation-1/approve',
        async ({ request }) => {
          approvalBody = await request.json();
          operationStatus = 'approved';
          return HttpResponse.json({
            duplicate: false,
            operation: operation(),
            remote_write_performed: false,
          });
        },
      ),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '旧系统上传' }));
    expect(await screen.findByText('260187115-1')).toBeInTheDocument();
    expect(screen.getByText('源检验编号')).toBeInTheDocument();
    expect(screen.getByText('260187115')).toBeInTheDocument();
    expect(screen.getByText('辜惠珊')).toBeInTheDocument();
    expect(screen.getByText('260187115-根数法.xls')).toBeInTheDocument();
    expect(screen.getByText('批准可能触发真实的旧系统写入')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '核对并批准预检单' }));
    await user.type(
      screen.getByRole('textbox', { name: '确认样品编号' }),
      '260187115-1',
    );
    await user.type(
      screen.getByRole('textbox', { name: '审核备注' }),
      '只批准本地预检',
    );
    await user.click(
      screen.getByRole('button', { name: '批准并进入连接器队列' }),
    );

    await waitFor(() => expect(approvalBody).toEqual({
      approved: true,
      payload_checksum: 'a'.repeat(64),
      confirmed_sample_number: '260187115-1',
      note: '只批准本地预检',
    }));
    expect(await screen.findByText('已批准，等待连接器')).toBeInTheDocument();
  });

  it('同 revision 的 SSE 快照刷新保留未保存选择，revision 更新后恢复服务端草稿', async () => {
    let revision = 2;
    let draftData = {};
    let runRequests = 0;
    const resultFiles = [
      {
        id: 'candidate-refresh-1',
        name: '26X909953-第一次.xls',
        read_status: 'succeeded',
        result: {
          parts: [{
            name: null,
            label: '结果1',
            components: [{ name: '棉', content: 100 }],
          }],
          remarks: [],
          images: [],
        },
      },
      {
        id: 'candidate-refresh-2',
        name: '26X909953-复核.xls',
        read_status: 'succeeded',
        result: {
          parts: [{
            name: null,
            label: '结果1',
            components: [{ name: '粘纤', content: 100 }],
          }],
          remarks: [],
          images: [],
        },
      },
    ];
    const runPayload = () => ({
      ...completedRun,
      status: 'waiting_human',
      output_data: {},
      nodes: [{
        id: 'node-run-select-results',
        node_id: 'select-results',
        node_type: 'human.file_selection',
        name: '确认需要的结果',
        status: 'waiting_human',
        input_data: { files: resultFiles },
      }],
      human_tasks: [{
        id: 'task-result-refresh',
        run_id: 'run-1',
        node_id: 'select-results',
        title: '确认需要的结果',
        description: '选择需要的文件并指定主单',
        status: 'claimed',
        revision,
        claimed_by_id: 'u-1',
        draft_data: draftData,
        form_schema: {},
      }],
      definition: {
        ...completedRun.definition,
        nodes: [{
          id: 'select-results',
          type: 'human.file_selection',
          type_version: 1,
          name: '确认需要的结果',
          config: {
            allow_primary: true,
            require_primary: true,
            presentation: 'result_files',
          },
          input_mapping: {},
          ui: { x: 0, y: 0 },
        }],
      },
    });

    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.read', 'workflow.run', 'human_task.handle', 'file.read'],
        },
      })),
      http.get('/api/execution/v1/runs/run-1', () => {
        runRequests += 1;
        return HttpResponse.json(runPayload());
      }),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('26X909953-第一次.xls')).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('checkbox', { name: '需要' })[0]);
    expect(screen.getAllByRole('checkbox', { name: '需要' })[0]).toBeChecked();
    expect(screen.getAllByRole('radio', { name: /设为主单/ })[0]).toBeChecked();

    const eventOptions = useExecutionEvents.mock.calls.at(-1)[1];
    act(() => {
      eventOptions.onEvent({
        id: '10',
        type: 'node.updated',
        data: { sequence: 10 },
      });
    });
    await waitFor(() => expect(runRequests).toBeGreaterThanOrEqual(2));
    expect(screen.getAllByRole('checkbox', { name: '需要' })[0]).toBeChecked();
    expect(screen.getAllByRole('radio', { name: /设为主单/ })[0]).toBeChecked();

    revision = 3;
    draftData = {
      selected_files: ['candidate-refresh-2'],
      primary_file_id: 'candidate-refresh-2',
    };
    act(() => {
      eventOptions.onEvent({
        id: '11',
        type: 'human_task.draft_saved',
        data: { sequence: 11 },
      });
    });
    await waitFor(() => expect(runRequests).toBeGreaterThanOrEqual(3));
    await waitFor(() => {
      const needed = screen.getAllByRole('checkbox', { name: '需要' });
      expect(needed[0]).not.toBeChecked();
      expect(needed[1]).toBeChecked();
      expect(screen.getAllByRole('radio', { name: /设为主单/ })[1]).toBeChecked();
    });
  });

  it('终态运行可以分页加载更早动态并按序号去重合并', async () => {
    const recentEvents = Array.from({ length: 100 }, (_, index) => ({
      sequence: index + 101,
      event_id: `recent-${index}`,
      type: `event.recent.${index}`,
      payload: { message: `最近动态 ${index}` },
      occurred_at: `2026-07-25T08:${String(index % 60).padStart(2, '0')}:00+00:00`,
    }));
    let historyRequest = null;
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.read', 'workflow.run', 'human_task.handle'],
        },
      })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        events: recentEvents,
        event_history: {
          has_more: true,
          cursors: { before_id: 101, after_id: 200 },
        },
      })),
      http.get('/api/execution/v1/runs/run-1/event-history', ({ request }) => {
        const url = new URL(request.url);
        historyRequest = {
          beforeId: url.searchParams.get('before_id'),
          limit: url.searchParams.get('limit'),
        };
        return HttpResponse.json({
          items: [
            {
              sequence: 100,
              event_id: 'older-100',
              type: 'event.older',
              payload: { message: '更早动态' },
              occurred_at: '2026-07-25T07:59:00+00:00',
            },
            {
              ...recentEvents[0],
              type: 'event.duplicate-should-not-remain',
            },
          ],
          has_more: false,
          cursors: { before_id: 100, after_id: 101 },
        });
      }),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '时间线 100' }));
    await user.click(screen.getByRole('button', { name: '加载更早动态' }));

    expect(await screen.findByText('event.older')).toBeInTheDocument();
    expect(screen.getByText('更早动态')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '时间线 101' })).toBeInTheDocument();
    expect(screen.getAllByText('event.recent.0')).toHaveLength(1);
    expect(screen.queryByText('event.duplicate-should-not-remain')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '加载更早动态' })).not.toBeInTheDocument();
    expect(historyRequest).toEqual({ beforeId: '101', limit: '100' });
  });

  it('工作台复用安全人工任务卡并展示发布确认上下文', async () => {
    const approvalContext = {
      approved: true,
      mutation_id: 'mutation-safe-1',
      working_copy: {
        root_id: 'execution_staging',
        relative_path: 'run-1/result.xlsx',
        content_sha256: 'a'.repeat(64),
      },
      target: {
        root_id: 'execution_publish',
        relative_path: '26X910095-1/result.xlsx',
      },
      change_plan_checksum: 'b'.repeat(64),
    };
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.run', 'human_task.handle'],
        },
      })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        nodes: [{
          id: 'node-run-confirm',
          node_id: 'confirm',
          node_type: 'human.confirm',
          name: '确认发布',
          status: 'waiting_human',
          input_data: { approval_context: approvalContext },
        }],
        human_tasks: [{
          id: 'task-confirm',
          run_id: 'run-1',
          node_id: 'confirm',
          title: '确认发布',
          description: '核对后确认',
          status: 'claimed',
          revision: 2,
          claimed_by_id: 'another-user',
          form_schema: {
            type: 'object',
            required: ['approved'],
            properties: {
              approved: {
                type: 'boolean',
                const: true,
                title: '我已确认发布',
                validation_message: '请先勾选确认已核对变更计划',
              },
            },
          },
        }],
        definition: {
          ...completedRun.definition,
          nodes: [{
            id: 'confirm',
            type: 'human.confirm',
            type_version: 1,
            name: '确认发布',
            config: {},
            input_mapping: {},
            ui: { x: 0, y: 0 },
          }],
        },
      })),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('该任务已由其他人员领取')).toBeInTheDocument();
    expect(screen.getByText('mutation-safe-1')).toBeInTheDocument();
    expect(screen.getByText('execution_publish / 26X910095-1/result.xlsx')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认提交' })).not.toBeInTheDocument();
  });

  it('显示运行级错误、变更计划、核对结果和发布回执', async () => {
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.run', 'file.read', 'file.write', 'file.publish'],
        },
      })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        error: {
          code: 'controlled_write_failed',
          message: '写入流程需要人工检查',
        },
        definition: {
          ...completedRun.definition,
          nodes: [{
            id: 'copy',
            type: 'workbook.copy',
            type_version: 1,
            name: '创建工作副本',
            config: { staging_root_id: 'execution_staging' },
            input_mapping: {},
            ui: { x: 0, y: 0 },
          }],
        },
      })),
      http.get('/api/execution/v1/runs/run-1/mutations', () => HttpResponse.json({
        items: [{
          id: 'mutation-row-1',
          mutation_id: 'mutation-1',
          run_id: 'run-1',
          status: 'published',
          source: {
            root_id: 'inspection_records',
            relative_path: 'source.xlsx',
          },
          working_copy: {
            root_id: 'execution_staging',
            relative_path: 'run-1/source.xlsx',
          },
          change_plan: {
            writes: [{ sheet: '记录', cell: 'B2', value: 12 }],
          },
          verification_result: {
            verified: true,
            approval_context: {
              approved: true,
              mutation_id: 'mutation-1',
              working_copy: {
                root_id: 'execution_staging',
                relative_path: 'run-1/source.xlsx',
                content_sha256: 'c'.repeat(64),
              },
              target: {
                root_id: 'execution_publish',
                relative_path: 'result.xlsx',
              },
              change_plan_checksum: 'd'.repeat(64),
            },
          },
          publish_receipts: [{
            id: 'receipt-1',
            target_root_id: 'execution_publish',
            target_relative_path: 'result.xlsx',
            content_sha256: 'c'.repeat(64),
            status: 'published',
          }],
          updated_at: '2026-07-25T08:00:00Z',
        }],
      })),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('写入流程需要人工检查')).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('tab', { name: '文件变更' }));
    expect(await screen.findByText('mutation-1')).toBeInTheDocument();
    await userEvent.setup().click(screen.getByText('变更预检清单'));
    expect(await screen.findByText(/"cell": "B2"/)).toBeInTheDocument();
    expect(screen.getByText('发布回执')).toBeInTheDocument();
    expect(screen.getByText('execution_publish / result.xlsx')).toBeInTheDocument();
  });

  it('仅在实际执行路径到达后推进写入阶段，并显式绑定 node_id', async () => {
    const preflight = vi.fn();
    let copyStatus = 'pending';
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'u-1',
          username: 'operator',
          display_name: '检验员',
          role: 'user',
          permissions: ['workflow.run', 'file.read', 'file.write'],
        },
      })),
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'csrf-write' })),
      http.get('/api/execution/v1/runs/run-1', () => HttpResponse.json({
        ...completedRun,
        status: 'running',
        input_data: {
          ...completedRun.input_data,
          mutation_id: 'mutation-path-1',
          source: {
            root_id: 'inspection_records',
            relative_path: '2026-麻棉/source.xlsx',
          },
        },
        nodes: [{
          id: 'node-run-copy-1',
          node_id: 'copy-workbook',
          node_type: 'workbook.copy',
          status: copyStatus,
          input_data: {
            mutation_id: 'mutation-path-1',
            source: {
              root_id: 'inspection_records',
              relative_path: '2026-麻棉/source.xlsx',
            },
          },
        }],
        definition: {
          ...completedRun.definition,
          nodes: [{
            id: 'copy-workbook',
            type: 'workbook.copy',
            type_version: 1,
            name: '创建工作副本',
            config: { staging_root_id: 'execution_staging' },
            input_mapping: {},
            ui: { x: 0, y: 0 },
          }],
        },
      })),
      http.get('/api/execution/v1/runs/run-1/mutations', () =>
        HttpResponse.json({ items: [] })),
      http.post('/api/execution/v1/runs/run-1/mutations/preflight', async ({ request }) => {
        preflight(await request.json());
        return HttpResponse.json({
          mutation: {
            mutation_id: 'mutation-path-1',
            status: 'planned',
          },
        }, { status: 201 });
      }),
    );

    render(
      <MemoryRouter
        initialEntries={['/execution/runs/run-1']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ExecutionAuthProvider>
          <Routes>
            <Route path="/execution/runs/:runId" element={<ExecutionRunWorkspace />} />
          </Routes>
        </ExecutionAuthProvider>
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '文件变更' }));
    await screen.findByText('尚未创建文件变更计划');
    expect(await screen.findByRole('button', { name: /创建写入预检/ })).toBeDisabled();
    expect(screen.getByText('等待工作副本节点进入实际执行路径')).toBeInTheDocument();

    copyStatus = 'ready';
    await user.click(screen.getAllByRole('button', { name: /刷新/ })[0]);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /创建写入预检/ })).toBeEnabled();
    });
    await user.click(screen.getByRole('button', { name: /创建写入预检/ }));

    await waitFor(() => {
      expect(preflight).toHaveBeenCalledWith({
        mutation_id: 'mutation-path-1',
        source: {
          root_id: 'inspection_records',
          relative_path: '2026-麻棉/source.xlsx',
        },
        node_id: 'copy-workbook',
      });
    });
  });
});
