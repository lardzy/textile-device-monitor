import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import {
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import {
  beforeEach,
  describe,
  expect,
  it,
} from 'vitest';
import { server } from '../../../tests/testServer';
import { resetExecutionRunRequestCache } from '../../utils/executionRunRequest';
import { ExecutionAuthProvider } from './ExecutionAuthContext';
import ExecutionRunPreparation from './ExecutionRunPreparation';

const workflow = {
  id: 'wf-count',
  name: '再生纤-根数法',
  category: { id: 'regenerated', name: '再生纤' },
  published_version: 3,
  is_enabled: true,
  availability: { available: true },
  input_schema: {
    type: 'object',
    properties: {
      inspection_number: { type: 'string', title: '检验编号' },
      target_sample_number: { type: 'string', title: '目标样品编号' },
      sample_count: { type: 'integer', title: '样品数量', minimum: 1 },
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
  published_definition: {
    schema_version: '1.0',
    metadata: { name: '再生纤-根数法' },
    input_schema: {
      type: 'object',
      properties: {
        inspection_number: { type: 'string', title: '检验编号' },
        sample_count: { type: 'integer', title: '样品数量', minimum: 1 },
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
          default: 'normal',
        },
      },
    },
    nodes: [
      { id: 'start', type: 'core.start', name: '开始', ui: { x: 0, y: 0 } },
      {
        id: 'query',
        type: 'file.regenerated_fiber_count_method',
        name: '再生纤-根数法',
        ui: { x: 260, y: 0 },
      },
      { id: 'end', type: 'core.end', name: '结束', ui: { x: 520, y: 0 } },
    ],
    edges: [
      { id: 'start-query', source: 'start', target: 'query' },
      { id: 'query-end', source: 'query', target: 'end' },
    ],
  },
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

const renderPreparation = (initialEntry = '/execution/workflows/wf-count/start') => render(
  <MemoryRouter
    initialEntries={[initialEntry]}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <ExecutionAuthProvider>
      <Routes>
        <Route
          path="/execution/workflows/:workflowId/start"
          element={(
            <>
              <ExecutionRunPreparation />
              <LocationProbe />
            </>
          )}
        />
        <Route path="/execution/runs/:runId" element={<div>运行工作台已打开</div>} />
      </Routes>
    </ExecutionAuthProvider>
  </MemoryRouter>,
);

const installHandlers = runHandler => server.use(
  http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
    id: 'u-1',
    username: 'operator',
    display_name: '检验员',
    role: 'user',
  })),
  http.get('/api/execution/v1/workflows/wf-count', () => HttpResponse.json(workflow)),
  http.get('/api/execution/v1/auth/csrf', () => HttpResponse.json({
    csrf_token: 'csrf-test',
  })),
  http.post('/api/execution/v1/runs', runHandler || (async ({ request }) => {
    const body = await request.json();
    expect(body.workflow_id).toBe('wf-count');
    expect(body.inspection_number).toBe('260162847');
    expect(body.input_data.inspection_number).toBe('260162847');
    expect(body.input_data.sample_count).toBe(2);
    expect(body.global_data.priority).toBe('normal');
    expect(body.idempotency_key).toBeTruthy();
    return HttpResponse.json({ run: { id: 'run-1' } });
  })),
);

describe('ExecutionRunPreparation', () => {
  beforeEach(() => {
    resetExecutionRunRequestCache();
  });

  it('允许无编号查看流程，补齐必填信息后才创建运行', async () => {
    installHandlers();
    const user = userEvent.setup();
    renderPreparation();

    expect(await screen.findByText('尚未创建运行记录')).toBeInTheDocument();
    expect(screen.getByText('已发布流程')).toBeInTheDocument();
    expect(screen.getAllByText('再生纤-根数法').length).toBeGreaterThan(0);
    expect(screen.getByRole('textbox', { name: '检验编号' })).toHaveValue('');

    await user.click(screen.getByRole('button', { name: /确认并开始执行/ }));
    expect(await screen.findByText('请填写检验编号')).toBeInTheDocument();

    await user.type(screen.getByRole('textbox', { name: '检验编号' }), '260162847');
    await user.type(screen.getByRole('spinbutton', { name: '样品数量' }), '2');
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent(
        '/execution/workflows/wf-count/start?number=260162847',
      );
    });
    await user.click(screen.getByRole('button', { name: /确认并开始执行/ }));

    expect(await screen.findByText('运行工作台已打开')).toBeInTheDocument();
  });

  it('目录带入的编号可修改，网络失败重试复用同一个幂等键', async () => {
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
    renderPreparation('/execution/workflows/wf-count/start?number=260162847');

    expect(await screen.findByRole('textbox', { name: '检验编号' }))
      .toHaveValue('260162847');
    await user.type(screen.getByRole('spinbutton', { name: '样品数量' }), '2');
    await user.click(screen.getByRole('button', { name: /确认并开始执行/ }));
    await waitFor(() => expect(keys).toHaveLength(1));
    await waitFor(() => expect(
      screen.getByRole('button', { name: /确认并开始执行/ }),
    ).toBeEnabled());

    await user.click(screen.getByRole('button', { name: /确认并开始执行/ }));

    expect(await screen.findByText('运行工作台已打开')).toBeInTheDocument();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it('填写目标样品编号时随运行创建一并提交', async () => {
    installHandlers(async ({ request }) => {
      const body = await request.json();
      expect(body.inspection_number).toBe('260187115');
      expect(body.target_sample_number).toBe('260187115-1');
      expect(body.input_data.inspection_number).toBe('260187115');
      expect(body.input_data.target_sample_number).toBe('260187115-1');
      return HttpResponse.json({ run: { id: 'run-target' } });
    });
    const user = userEvent.setup();
    renderPreparation();

    await user.type(
      await screen.findByRole('textbox', { name: '检验编号' }),
      '260187115',
    );
    await user.type(screen.getByRole('spinbutton', { name: '样品数量' }), '1');
    await user.type(
      screen.getByRole('textbox', { name: '目标样品编号' }),
      '260187115-1',
    );
    await user.click(screen.getByRole('button', { name: /确认并开始执行/ }));

    expect(await screen.findByText('运行工作台已打开')).toBeInTheDocument();
  });
});
