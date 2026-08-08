import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../../tests/testServer';
import { ExecutionAuthProvider } from './ExecutionAuthContext';
import ExecutionTaskInbox from './ExecutionTaskInbox';

const openTask = {
  id: 'task-1',
  run_id: 'run-1',
  node_id: 'select-files',
  title: '选择原始记录',
  description: '从候选资料中选择本次使用的原始记录',
  status: 'open',
  revision: 1,
  candidate_role: 'reviewer',
  form_schema: {
    type: 'object',
    properties: {
      remark: { type: 'string', title: '处理备注' },
    },
  },
  created_at: '2026-07-25T08:00:00Z',
};

const detailPayload = task => ({
  task,
  run: {
    id: 'run-1',
    inspection_number: '26X910095-1',
    workflow_name: '特种毛原始记录处理',
  },
  workflow: { id: 'wf-1', name: '特种毛原始记录处理' },
  node_run: {
    node_id: 'select-files',
    input_data: {
      candidates: [
        {
          id: 'candidate-1',
          root_id: 'inspection_records',
          relative_path: '2026-特种毛/26X910095-1.xlsx',
          name: '26X910095-1.xlsx',
        },
      ],
    },
  },
});

const renderInbox = () => render(
  <MemoryRouter
    initialEntries={['/execution/tasks']}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <ExecutionAuthProvider>
      <Routes>
        <Route path="/execution/tasks" element={<ExecutionTaskInbox />} />
        <Route path="/execution/tasks/:taskId" element={<ExecutionTaskInbox />} />
      </Routes>
    </ExecutionAuthProvider>
  </MemoryRouter>,
);

describe('ExecutionTaskInbox', () => {
  beforeEach(() => {
    let task = { ...openTask };
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'reviewer-1',
          username: 'reviewer',
          display_name: '复核员',
          role: 'user',
          permissions: ['human_task.handle'],
        },
      })),
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'csrf-test' })),
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [task] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json(detailPayload(task))),
      http.post('/api/execution/v1/human-tasks/task-1/claim', () => {
        task = {
          ...task,
          status: 'claimed',
          revision: 2,
          claimed_by_id: 'reviewer-1',
        };
        return HttpResponse.json(task);
      }),
    );
  });

  it('显示跨用户待办，并通过详情领取任务', async () => {
    const user = userEvent.setup();
    renderInbox();

    expect(await screen.findByText('选择原始记录')).toBeInTheDocument();
    await user.click(screen.getByText('选择原始记录'));
    await user.click(await screen.findByRole('button', { name: '领取并处理' }));

    expect(await screen.findByText('26X910095-1.xlsx')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '确认提交' })).toBeInTheDocument();
  });

  it('文件选择仅提交服务端签发的稳定候选 ID', async () => {
    const submitted = vi.fn();
    server.use(
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json(detailPayload({
          ...openTask,
          status: 'claimed',
          revision: 2,
          claimed_by_id: 'reviewer-1',
        }))),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        const body = await request.json();
        submitted(body);
        return HttpResponse.json({
          ...openTask,
          status: 'completed',
          revision: 3,
        });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('选择原始记录'));
    const detail = await screen.findByText('26X910095-1.xlsx');
    // 仅一份候选时表单已自动选中，无需再点击
    expect(detail.closest('label').querySelector('input')).toBeChecked();
    await user.type(screen.getByRole('textbox', { name: '处理备注' }), '已核对');
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    const payload = submitted.mock.calls[0][0];
    expect(payload.data.selected_files).toEqual(['candidate-1']);
    expect(payload.data.selected_files[0]).not.toEqual(expect.objectContaining({
      relative_path: expect.anything(),
    }));
    expect(within(document.body).queryByText('发布路径')).not.toBeInTheDocument();
  });

  it('超长候选文件名保留完整核对信息，但不在窄栏重复展示完整路径', async () => {
    const longName = '260144785-这是一个用于验证窄栏布局不会横向溢出的超长面积法定量试验原始记录-新系统.xls';
    const longPath = `7月/${longName}`;
    const claimedTask = {
      ...openTask,
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    server.use(
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(claimedTask),
          node_run: {
            node_id: 'select-files',
            input_data: {
              candidates: [{
                id: 'candidate-long',
                relative_path: longPath,
                name: longName,
                suffix: '.xls',
              }],
            },
          },
        })),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('选择原始记录'));

    const fileName = await screen.findByTitle(longName);
    expect(fileName).toHaveTextContent(longName);
    expect(screen.getByText('7月 · .xls')).toBeInTheDocument();
    expect(screen.getByTitle(`${longPath} · .xls`)).toBeInTheDocument();
    expect(screen.queryByText(longPath)).not.toBeInTheDocument();
    // 仅一份候选时表单已自动选中，无需再点击
    expect(fileName.closest('label').querySelector('input')).toBeChecked();
  });

  it('人工任务发生 409 时刷新详情，避免继续使用旧 revision', async () => {
    let detailRequests = 0;
    server.use(
      http.get('/api/execution/v1/human-tasks/task-1', () => {
        detailRequests += 1;
        return HttpResponse.json(detailPayload({
          ...openTask,
          status: 'claimed',
          revision: detailRequests > 1 ? 3 : 2,
          claimed_by_id: 'reviewer-1',
        }));
      }),
      http.put('/api/execution/v1/human-tasks/task-1/draft', () =>
        HttpResponse.json({
          code: 'human_task_revision_conflict',
          message: '任务已被其他人员更新',
          details: { current_revision: 3 },
          request_id: 'request-conflict-1',
        }, { status: 409 })),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('选择原始记录'));
    await user.click(await screen.findByRole('button', { name: '保存草稿' }));

    await waitFor(() => expect(detailRequests).toBeGreaterThanOrEqual(2));
    expect(await screen.findByText('任务已被其他人员更新，正在刷新最新状态')).toBeInTheDocument();
  });

  it('发布确认前读取服务器保存的完整变更清单和核对结果', async () => {
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
    const approvalTask = {
      ...openTask,
      title: '确认发布',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
      form_schema: {
        type: 'object',
        required: ['approved'],
        properties: {
          approved: {
            type: 'boolean',
            const: true,
            title: '我已核对并确认发布',
            validation_message: '请先勾选确认已核对变更计划',
          },
        },
      },
    };
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'reviewer-1',
          username: 'reviewer',
          display_name: '复核员',
          role: 'user',
          permissions: ['human_task.handle', 'file.read'],
        },
      })),
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [approvalTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(approvalTask),
          node_run: {
            node_id: 'confirm-publish',
            input_data: { approval_context: approvalContext },
          },
        })),
      http.get('/api/execution/v1/runs/run-1/mutations/mutation-safe-1', () =>
        HttpResponse.json({
          mutation: {
            mutation_id: 'mutation-safe-1',
            status: 'verified',
            change_plan: {
              writes: [{ sheet: '原始记录', cell: 'D8', value: 42 }],
            },
            verification_result: {
              verified: true,
              checked_cells: 1,
            },
          },
        })),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('确认发布'));
    expect(await screen.findByText('查看完整变更预检清单')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认提交' })).toBeEnabled();

    await user.click(screen.getByText('查看完整变更预检清单'));
    expect(await screen.findByText(/"cell": "D8"/)).toBeInTheDocument();
    await user.click(screen.getByText('查看保存后重读核对结果'));
    expect(await screen.findByText(/"checked_cells": 1/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认提交' }));
    expect(
      await screen.findByText('请先勾选确认已核对变更计划'),
    ).toBeInTheDocument();
  });

  it('按文件展示读取结果并提交需要的文件与主单', async () => {
    const submitted = vi.fn();
    const resultTask = {
      ...openTask,
      title: '核对再生纤结果',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    const resultFiles = [
      {
        id: 'candidate-1',
        name: '26X909953-第一次.xls',
        relative_path: '7月/26X909953-第一次.xls',
        read_status: 'succeeded',
        result: {
          parts: [{
            name: '面料',
            components: [
              { name: '棉', content: 97.6 },
              { name: '粘纤', content: 2.4 },
            ],
          }],
          remarks: [{ cell: 'B27', text: '结果仅供复核' }],
          images: [{
            artifact_id: 'image-artifact-1',
            filename: '插图1.png',
          }, {
            artifact_id: 'image-artifact-2',
            filename: '插图2.png',
          }],
        },
      },
      {
        file_index_entry_id: 'candidate-2',
        name: '26X909953-复核.xls',
        relative_path: '7月/26X909953-复核.xls',
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
        id: 'candidate-image-warning',
        name: '26X909953-图片异常.xls',
        relative_path: '7月/26X909953-图片异常.xls',
        read_status: 'succeeded',
        result: {
          parts: [{
            name: '袖口',
            components: [{ name: '棉', content: 100 }],
          }],
          remarks: [],
          images: [],
          warnings: [{
            code: 'legacy_image_conversion_failed',
            message: '旧版工作簿插图转换失败',
            details: { return_code: 1 },
          }],
        },
      },
      {
        id: 'candidate-failed',
        name: '26X909953-损坏.xls',
        relative_path: '7月/26X909953-损坏.xls',
        read_status: 'failed',
        error: {
          code: 'result_workbook_read_failed',
          message: '工作簿结果读取失败',
        },
      },
    ];
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [resultTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(resultTask),
          node_run: {
            node_id: 'select-results',
            input_data: { files: resultFiles },
          },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({
          ...resultTask,
          status: 'completed',
          revision: 3,
        });
      }),
      http.get('/api/execution/v1/artifacts/image-artifact-1/preview', () =>
        new HttpResponse(new Uint8Array(), {
          headers: { 'Content-Type': 'image/png' },
        })),
      http.get('/api/execution/v1/artifacts/image-artifact-2/preview', () =>
        new HttpResponse(new Uint8Array(), {
          headers: { 'Content-Type': 'image/png' },
        })),
    );

    const user = userEvent.setup();
    renderInbox();
    await user.click(await screen.findByText('核对再生纤结果'));

    expect(await screen.findByText('面料')).toBeInTheDocument();
    expect(screen.getByText('97.6%')).toBeInTheDocument();
    expect(screen.getByText('粘纤')).toBeInTheDocument();
    expect(screen.getByText('结果仅供复核')).toBeInTheDocument();
    expect(screen.getByText('结果1')).toBeInTheDocument();
    expect(screen.getByText('未读取到表格插图')).toBeInTheDocument();
    expect(screen.getByText('旧版工作簿插图转换失败')).toBeInTheDocument();
    expect(screen.getByText('legacy_image_conversion_failed')).toBeInTheDocument();
    expect(screen.getByText('{"return_code":1}')).toBeInTheDocument();
    expect(screen.getByText('图片读取异常')).toBeInTheDocument();
    expect(screen.getByText('工作簿结果读取失败')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '确认提交' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('请至少选择一项需要的文件');
    expect(submitted).not.toHaveBeenCalled();
    expect(screen.queryByText('人工任务操作失败')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /查看图片（2）/ }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('插图1.png');
    expect(screen.getByRole('dialog')).toHaveTextContent('插图2.png');
    await user.click(screen.getByRole('button', { name: 'Close' }));

    const needed = screen.getAllByRole('checkbox', { name: '需要' });
    expect(needed[3]).toBeDisabled();
    await user.click(needed[0]);
    let primaryChoices = screen.getAllByRole('radio', { name: /设为主单/ });
    expect(primaryChoices[0]).toBeChecked();
    await user.click(needed[1]);
    primaryChoices = screen.getAllByRole('radio', { name: /设为主单/ });
    expect(primaryChoices[0]).toBeChecked();
    await user.click(primaryChoices[1]);
    expect(primaryChoices[1]).toBeChecked();
    await user.click(needed[1]);
    primaryChoices = screen.getAllByRole('radio', { name: /设为主单/ });
    expect(primaryChoices[0]).toBeChecked();
    await user.click(needed[1]);
    await user.click(screen.getAllByRole('radio', { name: /设为主单/ })[1]);
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toMatchObject({
      selected_files: ['candidate-1', 'candidate-2'],
      primary_file_id: 'candidate-2',
    });
  });

  it('按顶层文件夹汇总电镜图片，支持大图切换并只提交稳定图片 ID', async () => {
    const submitted = vi.fn();
    const imageTask = {
      ...openTask,
      title: '选择纤维微观形貌图片',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    const inputData = {
      folder_selection_required: true,
      truncated: true,
      folders: [{
        id: 'folder-main',
        folder_name: '26A029794',
        relative_path: '26A029794',
      }, {
        id: 'folder-retake',
        folder_name: '26A029794-补拍',
        relative_path: '26A029794-补拍',
      }],
      images: [{
        id: 'image-bmp',
        folder_id: 'folder-main',
        folder_name: '26A029794',
        name: '26A029794_纵面_01.BMP',
        relative_path: '26A029794/纵面/26A029794_纵面_01.BMP',
        preview_url: '/preview/image-bmp',
      }, {
        id: 'image-jpeg',
        folder_id: 'folder-main',
        folder_name: '26A029794',
        name: '26A029794_横截面_02.JPEG',
        relative_path: '26A029794/横截面/26A029794_横截面_02.JPEG',
        preview_url: '/preview/image-jpeg',
      }, {
        id: 'image-png',
        folder_id: 'folder-retake',
        folder_name: '26A029794-补拍',
        name: '26A029794_补拍_03.PNG',
        relative_path: '26A029794-补拍/补拍/26A029794_补拍_03.PNG',
        preview_url: '/preview/image-png',
      }, ...Array.from({ length: 9 }, (_, index) => ({
        id: `image-extra-${index + 1}`,
        folder_id: 'folder-main',
        folder_name: '26A029794',
        name: `26A029794_补充_${index + 1}.jpg`,
        relative_path: `26A029794/补充/26A029794_补充_${index + 1}.jpg`,
        preview_url: `/preview/image-extra-${index + 1}`,
      }))],
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [imageTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(imageTask),
          node_run: {
            node_id: 'select-images',
            input_data: inputData,
          },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({
          ...imageTask,
          status: 'completed',
          revision: 3,
        });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('选择纤维微观形貌图片'));
    expect(screen.getByText('候选图片数量超过展示上限')).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '确认提交' }));
    expect(await screen.findByText('请至少选择一个结果文件夹', { exact: true }))
      .toBeInTheDocument();

    await user.click(screen.getByRole('checkbox', { name: /26A029794 11 张图片/ }));
    expect(await screen.findByText('26A029794_纵面_01.BMP')).toBeInTheDocument();
    expect(screen.getByText('26A029794_横截面_02.JPEG')).toBeInTheDocument();
    expect(screen.queryByText('26A029794_补拍_03.PNG')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '选择 26A029794_纵面_01.BMP' }));
    await user.click(screen.getByRole('button', { name: '选择 26A029794_横截面_02.JPEG' }));
    expect(screen.getByText('已选 2 / 10')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '查看大图 26A029794_纵面_01.BMP' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('26A029794_纵面_01.BMP');
    await user.click(screen.getByRole('button', { name: '下一张图片' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('26A029794_横截面_02.JPEG');
    await user.click(screen.getByRole('button', { name: 'Close' }));

    await user.click(screen.getByRole('button', { name: '选择当前图片' }));
    expect(await screen.findByText('已按当前顺序选择前 10 张图片')).toBeInTheDocument();
    expect(screen.getByText('已选 10 / 10')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '清空选择' }));
    await user.click(screen.getByRole('button', { name: '选择 26A029794_纵面_01.BMP' }));
    await user.click(screen.getByRole('button', { name: '选择 26A029794_横截面_02.JPEG' }));

    await user.click(screen.getByRole('checkbox', { name: /26A029794-补拍 1 张图片/ }));
    expect(await screen.findByText('26A029794_补拍_03.PNG')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toMatchObject({
      selected_folder_ids: ['folder-main', 'folder-retake'],
      selected_image_ids: ['image-bmp', 'image-jpeg'],
      primary_image_id: 'image-bmp',
    });
    expect(submitted.mock.calls[0][0].data).not.toHaveProperty('relative_path');
  });

  it('单个电镜结果文件夹自动选中并可立即选择图片', async () => {
    const submitted = vi.fn();
    const imageTask = {
      ...openTask,
      node_id: 'select-images',
      title: '选择单目录微观形貌图片',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    const inputData = {
      folder_selection_required: false,
      selected_folder_ids: ['folder-single'],
      task_validation_state: 'matched',
      task_cache_state: 'ready',
      folders: [{
        id: 'folder-single',
        name: '260111037',
        relative_path: '260111037',
      }],
      images: [{
        id: 'image-single',
        folder_id: 'folder-single',
        folder_name: '260111037',
        name: '500x_q09.bmp',
        relative_path: '260111037/500x_q09.bmp',
        preview_url: '/preview/image-single',
      }],
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [imageTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(imageTask),
          node_run: { node_id: 'select-images', input_data: inputData },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({ ...imageTask, status: 'completed', revision: 3 });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('选择单目录微观形貌图片'));
    const folder = await screen.findByRole('checkbox', { name: /260111037 1 张图片/ });
    await waitFor(() => expect(folder).toBeChecked());
    expect(screen.getByText('500x_q09.bmp')).toBeInTheDocument();

    await user.click(folder);
    expect(folder).not.toBeChecked();
    expect(screen.queryByText('500x_q09.bmp')).not.toBeInTheDocument();
    await user.click(folder);
    expect(folder).toBeChecked();
    await user.click(await screen.findByRole('button', { name: '选择 500x_q09.bmp' }));
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toMatchObject({
      selected_folder_ids: ['folder-single'],
      selected_image_ids: ['image-single'],
      primary_image_id: 'image-single',
    });
  });

  it('同 revision 的节点候选稍后到达时完成初始化且普通刷新保留本地选图', async () => {
    let detailRequests = 0;
    const imageTask = {
      ...openTask,
      node_id: 'select-images',
      title: '延迟返回的微观形貌图片',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    const inputData = {
      folder_selection_required: false,
      selected_folder_ids: ['folder-late'],
      task_validation_state: 'matched',
      task_cache_state: 'ready',
      folders: [{
        id: 'folder-late',
        name: '260111037',
        relative_path: '260111037',
      }],
      images: [{
        id: 'image-late',
        folder_id: 'folder-late',
        folder_name: '260111037',
        name: 'late.bmp',
        relative_path: '260111037/late.bmp',
        preview_url: '/preview/image-late',
      }],
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [imageTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () => {
        detailRequests += 1;
        const payload = detailPayload(imageTask);
        if (detailRequests === 1) {
          delete payload.node_run;
        } else {
          payload.node_run = { node_id: 'select-images', input_data: inputData };
        }
        return HttpResponse.json(payload);
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('延迟返回的微观形貌图片'));
    expect(await screen.findByText('任务上下文尚未返回')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /刷新/ }));

    const folder = await screen.findByRole('checkbox', { name: /260111037 1 张图片/ });
    expect(folder).toBeChecked();
    const image = await screen.findByRole('button', { name: '选择 late.bmp' });
    await user.click(image);
    expect(image).toHaveAttribute('aria-pressed', 'true');

    await user.click(screen.getByRole('button', { name: /刷新/ }));
    await waitFor(() => expect(detailRequests).toBeGreaterThanOrEqual(3));
    expect(screen.getByRole('button', { name: '取消选择 late.bmp' }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('旧系统任务快照由 pending 更新为 ready 后移除非阻塞提示', async () => {
    let releaseStatus;
    let statusRequests = 0;
    const statusGate = new Promise((resolve) => { releaseStatus = resolve; });
    const imageTask = {
      ...openTask,
      node_id: 'select-images',
      title: '等待任务快照的图片选择',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    const inputData = {
      folder_selection_required: false,
      selected_folder_ids: ['folder-pending'],
      task_validation_state: 'pending',
      task_cache_state: 'pending',
      missing_conditions: ['task_item_name', 'test_method'],
      folders: [{ id: 'folder-pending', name: '26X910095-1' }],
      images: [{
        id: 'image-pending',
        folder_id: 'folder-pending',
        name: 'pending.bmp',
        relative_path: '26X910095-1/pending.bmp',
      }],
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [imageTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(imageTask),
          node_run: { node_id: 'select-images', input_data: inputData },
        })),
      http.get(
        '/api/execution/v1/task-snapshots/26X910095-1/status',
        async () => {
          statusRequests += 1;
          await statusGate;
          return HttpResponse.json({
            cache_state: 'ready',
            refresh_status: 'succeeded',
            snapshot_available: true,
            matched_conditions: ['task_item_name', 'test_method'],
            missing_conditions: [],
            error_code: null,
          });
        },
      ),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('等待任务快照的图片选择'));
    expect(await screen.findByText('旧系统任务信息尚未就绪，不影响当前选图'))
      .toBeInTheDocument();
    await waitFor(() => expect(statusRequests).toBe(1));
    await act(async () => { releaseStatus(); });
    await waitFor(() => {
      expect(screen.queryByText('旧系统任务信息尚未就绪，不影响当前选图'))
        .not.toBeInTheDocument();
    });
  });

  it('微观形貌原始记录支持分词选择、手工修正和多值判定', async () => {
    const submitted = vi.fn();
    const recordTask = {
      ...openTask,
      title: '填写微观形貌原始记录',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [recordTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(recordTask),
          node_run: {
            node_id: 'microscopy-record-input',
            input_data: {
              task_kind: 'microscopy_record_input',
              inspection_number: '260061860',
              selected_image_ids: ['image-1', 'image-2', 'image-3'],
              projects: [{
                project_key: 'project-morphology',
                project_name: '纤维微观形貌',
                test_method: 'GB/T 36422-2018',
                sample_identify: '膜外侧，膜内侧、截面',
                give_judgement: '1',
              }],
              sample_name_analysis: {
                original: 'Surgicel-Fibrillar 止血纱布',
                candidates: ['Surgicel-Fibrillar', '氧化再生纤维素纤维'],
              },
              check_basis_options: ['GB/T 36422-2018', '客户要求'],
            },
          },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({ ...recordTask, status: 'completed', revision: 3 });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('填写微观形貌原始记录'));
    expect(await screen.findByText('260061860')).toBeInTheDocument();
    expect(screen.getByText('已选图片').parentElement).toHaveTextContent('3 张');
    expect(screen.getByText('Surgicel-Fibrillar 止血纱布')).toBeInTheDocument();
    expect(screen.getByText('指标要求与测试结果将在后续版本完善'))
      .toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '氧化再生纤维素纤维' }));
    const sampleName = screen.getByRole('textbox', { name: '写入样品名称' });
    expect(sampleName).toHaveValue('氧化再生纤维素纤维');
    await user.type(sampleName, '（纱布）');
    await user.click(screen.getByRole('radio', { name: '膜内侧' }));
    await user.click(screen.getByRole('combobox', { name: '判定依据' }));
    await user.click((await screen.findAllByText('客户要求')).at(-1));
    await user.click(screen.getByRole('radio', { name: '符合' }).closest('label'));
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toMatchObject({
      selected_project_key: 'project-morphology',
      sample_name: '氧化再生纤维素纤维（纱布）',
      sample_identity: '膜内侧',
      judge_basis: '客户要求',
      judgement: '符合',
    });
  });

  it('样品识别单值自动选用，不判否时隐藏判定区', async () => {
    const recordTask = {
      ...openTask,
      title: '核对微观形貌信息',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [recordTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(recordTask),
          node_run: {
            node_id: 'microscopy-record-input',
            input_data: {
              task_kind: 'microscopy_record_input',
              projects: [{
                project_key: 'project-one',
                project_name: '纤维微观形貌',
                sample_identify: '正面',
                give_judgement: '0',
              }],
              sample_name_analysis: {
                original: '止血材料',
                candidates: ['止血材料'],
              },
              selected_image_ids: ['image-1'],
            },
          },
        })),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('核对微观形貌信息'));
    expect(await screen.findByText('正面')).toBeInTheDocument();
    expect(screen.getAllByText('已自动选用')).toHaveLength(2);
    expect(screen.queryByText('判定信息')).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: '符合' })).not.toBeInTheDocument();
  });

  it('选择打印时会明确提示人工打印并绑定文件 SHA-256', async () => {
    const submitted = vi.fn();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    const sha256 = 'a'.repeat(64);
    const printTask = {
      ...openTask,
      title: '打印微观形貌原始记录',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [printTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(printTask),
          node_run: {
            node_id: 'microscopy-print',
            input_data: {
              task_kind: 'microscopy_print_confirmation',
              artifact: {
                id: 'artifact-record-1',
                name: '260061860-纤维微观形貌.xls',
                download_url: '/api/execution/v1/artifacts/artifact-record-1/download',
                preview_url: '/api/execution/v1/artifacts/artifact-record-1/preview',
                sha256,
              },
            },
          },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({ ...printTask, status: 'completed', revision: 3 });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('打印微观形貌原始记录'));
    expect(screen.getByText(sha256)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /下载工作簿/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认提交' }));
    expect(await screen.findByText('请选择打印原始记录或暂不打印')).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: /^打印 / }));
    expect(screen.getByText('打印需要在 Excel 中手动完成')).toBeInTheDocument();
    expect(screen.getByText(/网页不会自动打印/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /下载工作簿/ }))
      .toHaveAttribute('href', '/api/execution/v1/artifacts/artifact-record-1/download');
    await user.click(screen.getByRole('button', { name: /打开工作簿（手动打印）/ }));
    expect(openSpy).toHaveBeenCalledWith(
      '/api/execution/v1/artifacts/artifact-record-1/preview',
      '_blank',
      'noopener,noreferrer',
    );

    await user.click(screen.getByRole('button', { name: '确认提交' }));
    expect(await screen.findByText('请确认已在 Excel 中完成打印')).toBeInTheDocument();
    expect(submitted).not.toHaveBeenCalled();
    await user.click(screen.getByRole('checkbox', { name: '我确认已在 Excel 中完成打印' }));
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toMatchObject({
      print_decision: 'print',
      print_completed: true,
      artifact_sha256: sha256,
    });
    expect(submitted.mock.calls[0][0].data).not.toHaveProperty('printed');
    openSpy.mockRestore();
  });

  it('选择暂不打印时无需打开工作簿也可以继续', async () => {
    const submitted = vi.fn();
    const sha256 = 'b'.repeat(64);
    const printTask = {
      ...openTask,
      title: '确认是否打印原始记录',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [printTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(printTask),
          node_run: {
            node_id: 'microscopy-print',
            input_data: {
              task_kind: 'microscopy_print_confirmation',
              artifact: {
                id: 'artifact-record-2',
                name: '260061860-纤维微观形貌.xls',
                download_url: '/api/execution/v1/artifacts/artifact-record-2/download',
                sha256,
              },
            },
          },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({ ...printTask, status: 'completed', revision: 3 });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('确认是否打印原始记录'));
    await user.click(screen.getByRole('radio', { name: /^暂不打印 / }));
    expect(await screen.findByText('本次暂不打印')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /打开工作簿/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toEqual({
      print_decision: 'skip',
      artifact_sha256: sha256,
    });
  });

  it('旧版打印任务仍以 printed=true 完成确认', async () => {
    const submitted = vi.fn();
    const sha256 = 'c'.repeat(64);
    const printTask = {
      ...openTask,
      title: '打印原始记录',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
      form_schema: {
        type: 'object',
        properties: {
          printed: { type: 'boolean', const: true },
          artifact_sha256: { type: 'string' },
        },
        required: ['printed', 'artifact_sha256'],
        additionalProperties: false,
      },
    };
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [printTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(printTask),
          task: printTask,
          node_run: {
            node_id: 'microscopy-print',
            input_data: {
              task_kind: 'microscopy_print_confirmation',
              artifact: {
                id: 'artifact-record-legacy',
                name: '260061860-纤维微观形貌.xls',
                download_url: '/api/execution/v1/artifacts/artifact-record-legacy/download',
                sha256,
              },
            },
          },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({ ...printTask, status: 'completed', revision: 3 });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('打印原始记录'));
    expect(await screen.findByText('此任务由旧版本流程创建')).toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: '我确认已在 Excel 中完成打印' }));
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toEqual({
      printed: true,
      artifact_sha256: sha256,
    });
  });

  it('纸类候选展示 W32 结果并明确限制为单选', async () => {
    const submitted = vi.fn();
    const paperTask = {
      ...openTask,
      title: '选择纸类原始记录',
      description: '查看 Sheet1!W32 定性结果后选择一份原始记录。',
      status: 'claimed',
      revision: 2,
      claimed_by_id: 'reviewer-1',
      form_schema: { type: 'object', properties: {} },
    };
    const paperCandidates = [{
      id: 'paper-file-1',
      name: '26W006687-第一次.xls',
      relative_path: '26W006687/26W006687-第一次.xls',
      read_status: 'succeeded',
      qualitative_result: '木浆 100',
      result: {
        worksheet: 'Sheet1',
        cell: 'W32',
        w32_value: '木浆 100',
        qualitative_result: '木浆 100',
        unit: '%',
      },
    }, {
      id: 'paper-file-2',
      name: '26W006687-复核.xls',
      relative_path: '26W006687/26W006687-复核.xls',
      read_status: 'succeeded',
      qualitative_result: '草浆、木浆、竹浆',
      result: {
        worksheet: 'Sheet1',
        cell: 'W32',
        w32_value: '草浆、木浆、竹浆',
        qualitative_result: '草浆、木浆、竹浆',
        unit: '',
      },
    }];
    server.use(
      http.get('/api/execution/v1/human-tasks', () =>
        HttpResponse.json({ items: [paperTask] })),
      http.get('/api/execution/v1/human-tasks/task-1', () =>
        HttpResponse.json({
          ...detailPayload(paperTask),
          task: paperTask,
          run: {
            id: 'run-1',
            inspection_number: '26W006687',
            workflow_name: '纸、纸板和纸浆纤维鉴别分析 GB/T 4688-2020',
          },
          node_run: {
            node_id: 'select',
            node_type: 'human.file_selection',
            input_data: { candidates: paperCandidates },
          },
        })),
      http.post('/api/execution/v1/human-tasks/task-1/submit', async ({ request }) => {
        submitted(await request.json());
        return HttpResponse.json({ ...paperTask, status: 'completed', revision: 3 });
      }),
    );
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByText('选择纸类原始记录'));
    expect(await screen.findByText('纸类原始记录（2，单选）')).toBeInTheDocument();
    expect(screen.getAllByText('Sheet1!W32')).toHaveLength(2);
    expect(screen.getByText('木浆 100')).toBeInTheDocument();
    expect(screen.getByText('草浆、木浆、竹浆')).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: '需要' })).not.toBeInTheDocument();
    expect(screen.queryByRole('radio', { name: /设为主单/ })).not.toBeInTheDocument();

    const choices = screen.getAllByRole('radio', { name: /选择文件：/ });
    await user.click(choices[0]);
    expect(choices[0]).toBeChecked();
    await user.click(choices[1]);
    expect(choices[0]).not.toBeChecked();
    expect(choices[1]).toBeChecked();
    await user.click(screen.getByRole('button', { name: '确认提交' }));

    await waitFor(() => expect(submitted).toHaveBeenCalledTimes(1));
    expect(submitted.mock.calls[0][0].data).toMatchObject({
      selected_files: ['paper-file-2'],
      primary_file_id: 'paper-file-2',
    });
  });
});
