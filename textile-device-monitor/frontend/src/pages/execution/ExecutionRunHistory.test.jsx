import { MemoryRouter, Route, Routes } from 'react-router-dom';
import {
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { server } from '../../../tests/testServer';
import { ExecutionAuthProvider } from './ExecutionAuthContext';
import ExecutionRunHistory from './ExecutionRunHistory';

const activeRun = {
  id: 'run-active',
  workflow_id: 'wf-area',
  workflow_name: '再生纤-面积法',
  inspection_number: '260144785',
  status: 'waiting_human',
  mode: 'live',
  node_progress: { completed: 2, total: 5 },
  open_human_task_count: 1,
  created_by: { display_name: '李工' },
  updated_at: '2026-07-27T11:20:00+08:00',
};

const renderHistory = () => render(
  <MemoryRouter
    initialEntries={['/execution/runs']}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <ExecutionAuthProvider>
      <Routes>
        <Route path="/execution/runs" element={<ExecutionRunHistory />} />
        <Route path="/execution/runs/:runId" element={<div>运行工作台已恢复</div>} />
      </Routes>
    </ExecutionAuthProvider>
  </MemoryRouter>,
);

describe('ExecutionRunHistory', () => {
  it('默认显示进行中的运行，并可返回原工作台继续处理', async () => {
    const requested = vi.fn();
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        id: 'u-1',
        username: 'operator',
        display_name: '检验员',
        role: 'user',
      })),
      http.get('/api/execution/v1/runs', ({ request }) => {
        const url = new URL(request.url);
        requested({
          group: url.searchParams.get('status_group'),
          offset: url.searchParams.get('offset'),
          limit: url.searchParams.get('limit'),
        });
        return HttpResponse.json({
          items: [activeRun],
          total: 1,
          offset: 0,
          limit: 20,
        });
      }),
    );
    const user = userEvent.setup();
    renderHistory();

    expect(await screen.findByText('260144785')).toBeInTheDocument();
    expect(screen.getByText('再生纤-面积法')).toBeInTheDocument();
    expect(screen.getByText('1 项待办')).toBeInTheDocument();
    expect(screen.getByText(
      '“待办任务”只处理人工步骤；这里保存每次流程运行的完整入口。',
    )).toBeInTheDocument();
    expect(requested).toHaveBeenCalledWith({
      group: 'active',
      offset: '0',
      limit: '20',
    });

    await user.click(screen.getByRole('button', { name: /继续处理/ }));

    expect(await screen.findByText('运行工作台已恢复')).toBeInTheDocument();
  });

  it('编号查询与全部记录筛选由服务端分页执行', async () => {
    const requests = [];
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        id: 'u-1',
        username: 'operator',
        display_name: '检验员',
        role: 'user',
      })),
      http.get('/api/execution/v1/runs', ({ request }) => {
        const url = new URL(request.url);
        requests.push({
          group: url.searchParams.get('status_group'),
          number: url.searchParams.get('inspection_number'),
        });
        return HttpResponse.json({
          items: [],
          total: 0,
          offset: 0,
          limit: 20,
        });
      }),
    );
    const user = userEvent.setup();
    renderHistory();
    await screen.findByText('当前没有进行中的执行流程');

    await user.click(screen.getByText('全部记录'));
    await user.type(screen.getByRole('textbox', { name: '按检验编号查找' }), '260162847');
    await user.click(screen.getByRole('button', { name: /查询/ }));

    await waitFor(() => {
      expect(requests).toContainEqual({
        group: null,
        number: '260162847',
      });
    });
  });
});
