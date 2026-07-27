import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import {
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { server } from '../../../tests/testServer';
import { resetExecutionRunRequestCache } from '../../utils/executionRunRequest';
import { ExecutionAuthProvider } from './ExecutionAuthContext';
import ExecutionCatalog from './ExecutionCatalog';

const workflows = [
  {
    id: 'wf-wool',
    name: '特种毛原始记录处理',
    category: { id: 'wool', name: '特种毛' },
    published_version: 2,
    runnable: true,
    capabilities: ['read'],
    input_schema: {
      type: 'object',
      properties: {
        inspection_number: { type: 'string', title: '检验编号' },
        sample_count: { type: 'integer', title: '样品数量', minimum: 1 },
        remark: { type: 'string', title: '备注' },
      },
      required: ['inspection_number', 'sample_count'],
    },
    global_schema: {
      type: 'object',
      properties: {
        priority: {
          type: 'string',
          title: '优先级',
          enum: ['normal', 'urgent'],
          enumNames: ['普通', '紧急'],
          default: 'normal',
        },
      },
    },
  },
  {
    id: 'wf-cotton',
    name: '麻棉原始记录处理',
    category: { id: 'cotton', name: '麻棉' },
    published_version: 1,
    runnable: true,
    capabilities: ['write'],
  },
  {
    id: 'wf-regenerated',
    name: '再生纤原始记录处理',
    category: { id: 'regenerated', name: '再生纤' },
    published_version: 1,
    runnable: false,
    availability: { available: false, message: '数据根未配置' },
  },
];

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

const renderCatalog = (initialEntry = '/execution') => render(
  <MemoryRouter
    initialEntries={[initialEntry]}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <ExecutionAuthProvider>
      <Routes>
        <Route
          path="/execution"
          element={(
            <>
              <ExecutionCatalog />
              <LocationProbe />
            </>
          )}
        />
        <Route path="/execution/runs/:runId" element={<div>运行工作台已打开</div>} />
      </Routes>
    </ExecutionAuthProvider>
  </MemoryRouter>,
);

const installHandlers = (runHandler) => {
  server.use(
    http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
      id: 'u-1',
      username: 'operator',
      display_name: '检验员',
      role: 'user',
    })),
    http.get('/api/execution/v1/categories', () => HttpResponse.json({
      items: [
        { id: 'wool', name: '特种毛' },
        { id: 'cotton', name: '麻棉' },
        { id: 'regenerated', name: '再生纤' },
      ],
    })),
    http.get('/api/execution/v1/workflows', () => HttpResponse.json({ items: workflows })),
    http.get('/api/execution/v1/runs', () => HttpResponse.json({ items: [] })),
    http.get('/api/execution/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'csrf-test' })),
    http.post('/api/execution/v1/runs', runHandler || (async ({ request }) => {
      expect(request.headers.get('X-CSRF-Token')).toBe('csrf-test');
      const body = await request.json();
      expect(body.inspection_number).toBe('26X910095-1');
      expect(body.input_data.inspection_number).toBe('26X910095-1');
      expect(body.input_data.sample_count).toBe(2);
      expect(body.input_data.remark).toBe('首轮检测');
      expect(body.global_data.priority).toBe('normal');
      expect(body.idempotency_key).toBeTruthy();
      return HttpResponse.json({ id: 'run-1' });
    })),
  );
};

describe('ExecutionCatalog', () => {
  beforeEach(() => {
    resetExecutionRunRequestCache();
  });

  it('preserves the inspection number in the URL and groups real workflow rows', async () => {
    installHandlers();
    const user = userEvent.setup();
    renderCatalog('/execution?number=26X9');

    expect(await screen.findByText('特种毛原始记录处理')).toBeInTheDocument();
    expect(screen.getByText('麻棉原始记录处理')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '检验编号' })).toHaveValue('26X9');

    await user.clear(screen.getByRole('textbox', { name: '检验编号' }));
    await user.type(screen.getByRole('textbox', { name: '检验编号' }), '26X910095-1');
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent(
        '/execution?number=26X910095-1',
      );
    });
  });

  it('在创建前收集必填、选填和全局输入，成功后打开只读工作台', async () => {
    installHandlers();
    const user = userEvent.setup();
    renderCatalog('/execution?number=26X910095-1');

    const cardTitle = await screen.findByText('特种毛原始记录处理');
    const card = cardTitle.closest('.execution-workflow-card');
    await user.click(card.querySelector('button'));

    const dialog = await screen.findByRole('dialog', { name: /开始执行/ });
    expect(within(dialog).getByRole('textbox', { name: '检验编号' })).toHaveValue('26X910095-1');
    expect(within(dialog).getByRole('textbox', { name: '备注' })).toBeEnabled();
    expect(within(dialog).getByText('流程全局变量')).toBeInTheDocument();

    await user.type(within(dialog).getByRole('spinbutton', { name: '样品数量' }), '2');
    await user.type(within(dialog).getByRole('textbox', { name: '备注' }), '首轮检测');
    await user.click(within(dialog).getByRole('button', { name: '确认并开始' }));

    expect(await screen.findByText('运行工作台已打开')).toBeInTheDocument();
  });

  it('网络失败后重试相同载荷会复用 idempotency_key', async () => {
    const keys = [];
    let attempts = 0;
    installHandlers(async ({ request }) => {
      const body = await request.json();
      keys.push(body.idempotency_key);
      attempts += 1;
      if (attempts === 1) {
        return HttpResponse.error();
      }
      return HttpResponse.json({ run: { id: 'run-recovered' } });
    });
    const user = userEvent.setup();
    renderCatalog('/execution?number=26X910095-1');

    const cardTitle = await screen.findByText('特种毛原始记录处理');
    await user.click(cardTitle.closest('.execution-workflow-card').querySelector('button'));
    const dialog = await screen.findByRole('dialog', { name: /开始执行/ });
    await user.type(within(dialog).getByRole('spinbutton', { name: '样品数量' }), '2');
    await user.click(within(dialog).getByRole('button', { name: '确认并开始' }));
    await waitFor(() => expect(keys).toHaveLength(1));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '确认并开始' })).toBeEnabled());

    await user.click(within(dialog).getByRole('button', { name: '确认并开始' }));

    expect(await screen.findByText('运行工作台已打开')).toBeInTheDocument();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it('renders missing data roots as unavailable instead of inventing a fallback', async () => {
    installHandlers();
    renderCatalog('/execution?number=26X910095-1');

    expect(await screen.findByText('再生纤原始记录处理')).toBeInTheDocument();
    expect(screen.getByText('数据根未配置')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /暂不可运行/ })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '流程管理' })).not.toBeInTheDocument();
  });

  it('按服务端权限隐藏设计入口，并禁用无运行权限账号的开始按钮', async () => {
    const runsRequested = vi.fn();
    installHandlers();
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'viewer-1',
          username: 'viewer',
          display_name: '只读用户',
          role: 'user',
          permissions: ['workflow.read'],
        },
      })),
      http.get('/api/execution/v1/runs', () => {
        runsRequested();
        return HttpResponse.json({ items: [] });
      }),
    );
    renderCatalog('/execution?number=26X910095-1');

    expect(await screen.findByText('特种毛原始记录处理')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '流程管理' })).not.toBeInTheDocument();
    screen.getAllByRole('button', { name: /开始执行/ }).forEach((button) => {
      expect(button).toBeDisabled();
    });
    expect(runsRequested).not.toHaveBeenCalled();
  });
});
