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
        <Route
          path="/execution/workflows/:workflowId/start"
          element={(
            <>
              <div>执行准备页已打开</div>
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
    http.get('/api/execution/v1/catalog/recommendations', () => HttpResponse.json({
      items: workflows.map((workflow, index) => ({
        workflow_id: workflow.id,
        state: 'no_match',
        score: 0,
        max_score: 5,
        full_match: false,
        candidate_count: 0,
        matched_conditions: [],
        index_state: 'ready',
        rank: index,
      })),
    })),
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

  it('未输入编号也能先进入执行准备页，且不会创建空白运行', async () => {
    const runRequested = vi.fn();
    installHandlers(() => {
      runRequested();
      return HttpResponse.json({ id: 'unexpected-run' });
    });
    const user = userEvent.setup();
    renderCatalog('/execution');

    const cardTitle = await screen.findByText('特种毛原始记录处理');
    const card = cardTitle.closest('.execution-workflow-card');
    expect(card.querySelectorAll('.ant-card-actions > li')).toHaveLength(1);
    expect(card.querySelector('.execution-workflow-card__meta')).toHaveTextContent('已发布');
    expect(card.querySelector('.execution-workflow-card__meta')).not.toHaveTextContent('v2');
    await user.hover(card.querySelector('.execution-workflow-card__meta span'));
    expect(await screen.findByText('当前发布版本：v2')).toBeInTheDocument();
    await user.click(card.querySelector('button'));

    expect(await screen.findByText('执行准备页已打开')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent(
      '/execution/workflows/wf-wool/start',
    );
    expect(runRequested).not.toHaveBeenCalled();
  });

  it('把目录中的编号带入执行准备页 URL', async () => {
    installHandlers();
    const user = userEvent.setup();
    renderCatalog('/execution?number=26X910095-1');

    const cardTitle = await screen.findByText('特种毛原始记录处理');
    await user.click(cardTitle.closest('.execution-workflow-card').querySelector('button'));

    expect(await screen.findByText('执行准备页已打开')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent(
      '/execution/workflows/wf-wool/start?number=26X910095-1',
    );
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

  it('管理员的流程设计入口只放在账号菜单，不占用生产目录高频区域', async () => {
    installHandlers();
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'admin-1',
          username: 'admin',
          display_name: '执行管理员',
          role: 'admin',
        },
      })),
    );
    const user = userEvent.setup();
    renderCatalog('/execution');

    expect(await screen.findByText('特种毛原始记录处理')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '流程管理' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '设计' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /执行管理员/ }));

    expect(await screen.findByText('流程设计与发布')).toBeInTheDocument();
  });

  it('把类别作为软优先条件并使用稳定的 category.key 排序，不隐藏其它类别', async () => {
    installHandlers();
    const preferredCategories = [];
    const keyedWorkflows = workflows.map(workflow => ({
      ...workflow,
      category: {
        ...workflow.category,
        id: `category-${workflow.category.id}`,
        key: `${workflow.category.id}_records`,
      },
    }));
    server.use(
      http.get('/api/execution/v1/categories', () => HttpResponse.json({
        items: [
          { id: 'category-wool', key: 'wool_records', name: '特种毛' },
          { id: 'category-cotton', key: 'cotton_records', name: '麻棉' },
          { id: 'category-regenerated', key: 'regenerated_records', name: '再生纤' },
        ],
      })),
      http.get('/api/execution/v1/workflows', () => HttpResponse.json({
        items: keyedWorkflows,
      })),
      http.get('/api/execution/v1/catalog/recommendations', ({ request }) => {
        const url = new URL(request.url);
        preferredCategories.push(
          url.searchParams.getAll('preferred_categories'),
        );
        return HttpResponse.json({
          items: [
            {
              workflow_id: 'wf-cotton',
              state: 'partial_match',
              score: 1,
              max_score: 5,
              full_match: false,
              candidate_count: 0,
              matched_conditions: ['category'],
              index_state: 'ready',
              rank: 0,
            },
            {
              workflow_id: 'wf-wool',
              state: 'no_match',
              score: 0,
              max_score: 5,
              full_match: false,
              candidate_count: 0,
              matched_conditions: [],
              index_state: 'ready',
              rank: 1,
            },
            {
              workflow_id: 'wf-regenerated',
              state: 'no_match',
              score: 0,
              max_score: 5,
              full_match: false,
              candidate_count: 0,
              matched_conditions: [],
              index_state: 'ready',
              rank: 2,
            },
          ],
        });
      }),
    );

    const rendered = renderCatalog('/execution?category=cotton_records');

    expect(await screen.findByText('特种毛原始记录处理')).toBeInTheDocument();
    expect(screen.getByText('麻棉原始记录处理')).toBeInTheDocument();
    expect(screen.getByText('再生纤原始记录处理')).toBeInTheDocument();
    expect(await screen.findByText('匹配 1 项')).toBeInTheDocument();
    await waitFor(() => {
      expect(preferredCategories).toEqual([['cotton_records']]);
    });
    const groupTitles = [
      ...rendered.container.querySelectorAll('.execution-workflow-group h2'),
    ].map(element => element.textContent);
    expect(groupTitles).toEqual(['麻棉', '特种毛', '再生纤']);
  });

  it('清空推荐条件后恢复服务端目录的默认顺序', async () => {
    installHandlers();
    server.use(
      http.get('/api/execution/v1/catalog/recommendations', () => HttpResponse.json({
        items: [
          {
            workflow_id: 'wf-cotton',
            state: 'partial_match',
            score: 2,
            max_score: 5,
            full_match: false,
            candidate_count: 0,
            matched_conditions: ['source_root', 'filename'],
            index_state: 'ready',
            rank: 0,
          },
          {
            workflow_id: 'wf-wool',
            state: 'no_match',
            score: 0,
            max_score: 5,
            full_match: false,
            candidate_count: 0,
            matched_conditions: [],
            index_state: 'ready',
            rank: 1,
          },
        ],
      })),
    );
    const user = userEvent.setup();
    const rendered = renderCatalog('/execution?number=260144785');

    expect(await screen.findByText('匹配 2 项')).toBeInTheDocument();
    expect(
      rendered.container.querySelector('.execution-workflow-group h2'),
    ).toHaveTextContent('麻棉');

    await user.clear(screen.getByRole('textbox', { name: '检验编号' }));

    await waitFor(() => {
      expect(
        rendered.container.querySelector('.execution-workflow-group h2'),
      ).toHaveTextContent('特种毛');
    });
    expect(screen.getByText('可直接选择流程；输入编号后会进一步识别候选文件并优化排序。'))
      .toBeInTheDocument();
  });

  it('回车只立即刷新推荐并定位首张推荐卡片，不会自动创建运行', async () => {
    const runRequested = vi.fn();
    installHandlers(() => {
      runRequested();
      return HttpResponse.json({ id: 'unexpected-run' });
    });
    const recommendationRequested = vi.fn();
    server.use(
      http.get('/api/execution/v1/catalog/recommendations', () => {
        recommendationRequested();
        return HttpResponse.json({
          items: [
            {
              workflow_id: 'wf-wool',
              state: 'full_match',
              score: 4,
              max_score: 5,
              full_match: true,
              candidate_count: 1,
              candidate_preview: {
                name: '260162847-根数法-定量试验原始记录.xls',
                relative_path: '7月/260162847-根数法-定量试验原始记录.xls',
                suffix: '.xls',
              },
              matched_conditions: [
                'source_root',
                'filename',
                'worksheet',
                'content_range',
              ],
              index_state: 'ready',
              rank: 0,
            },
          ],
        });
      }),
    );
    const scrollSpy = vi.spyOn(
      window.HTMLElement.prototype,
      'scrollIntoView',
    );
    const user = userEvent.setup();
    renderCatalog('/execution?number=260162847');

    const input = await screen.findByRole('textbox', { name: '检验编号' });
    await user.click(input);
    await user.keyboard('{Enter}');

    expect(await screen.findByText('找到 1 个符合文件')).toBeInTheDocument();
    expect(screen.getByText(
      '示例文件：260162847-根数法-定量试验原始记录.xls',
    )).toBeInTheDocument();
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    expect(recommendationRequested).toHaveBeenCalledTimes(1);
    expect(runRequested).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog', { name: /开始执行/ })).not.toBeInTheDocument();
    scrollSpy.mockRestore();
  });

  it('快速更换编号时会忽略已过期的推荐响应', async () => {
    installHandlers();
    const requestedNumbers = [];
    server.use(
      http.get('/api/execution/v1/catalog/recommendations', async ({ request }) => {
        const inspectionNumber = new URL(request.url)
          .searchParams.get('inspection_number');
        requestedNumbers.push(inspectionNumber);
        if (inspectionNumber === '260144785') {
          await new Promise(resolve => setTimeout(resolve, 600));
          return HttpResponse.json({
            items: [{
              workflow_id: 'wf-cotton',
              state: 'full_match',
              score: 4,
              max_score: 5,
              full_match: true,
              candidate_count: 1,
              matched_conditions: [
                'source_root',
                'filename',
                'worksheet',
                'content_range',
              ],
              index_state: 'ready',
              rank: 0,
            }],
          });
        }
        return HttpResponse.json({
          items: [{
            workflow_id: 'wf-wool',
            state: 'partial_match',
            score: 2,
            max_score: 5,
            full_match: false,
            candidate_count: 0,
            matched_conditions: ['source_root', 'filename'],
            index_state: 'ready',
            rank: 0,
          }],
        });
      }),
    );
    const user = userEvent.setup();
    const rendered = renderCatalog('/execution?number=260144785');
    const input = await screen.findByRole('textbox', { name: '检验编号' });

    await waitFor(() => expect(requestedNumbers).toContain('260144785'));
    await user.clear(input);
    await user.type(input, '260162847');

    expect(await screen.findByText('匹配 2 项')).toBeInTheDocument();
    expect(
      rendered.container.querySelector('.execution-workflow-group h2'),
    ).toHaveTextContent('特种毛');
    await new Promise(resolve => setTimeout(resolve, 650));
    expect(screen.queryByText('找到 1 个符合文件')).not.toBeInTheDocument();
    expect(
      rendered.container.querySelector('.execution-workflow-group h2'),
    ).toHaveTextContent('特种毛');
  });

  it('推荐接口失败时保持完整目录和默认顺序，不阻断手动选择流程', async () => {
    installHandlers();
    server.use(
      http.get(
        '/api/execution/v1/catalog/recommendations',
        () => HttpResponse.json(
          { code: 'index_unavailable', message: '索引服务暂不可用' },
          { status: 503 },
        ),
      ),
    );
    const rendered = renderCatalog('/execution?number=260144785');

    expect(await screen.findByText(
      '推荐识别暂不可用，已保持默认顺序，您仍可手动选择流程。',
    )).toBeInTheDocument();
    expect(screen.getByText('特种毛原始记录处理')).toBeInTheDocument();
    expect(screen.getByText('麻棉原始记录处理')).toBeInTheDocument();
    expect(screen.getByText('再生纤原始记录处理')).toBeInTheDocument();
    expect(
      rendered.container.querySelector('.execution-workflow-group h2'),
    ).toHaveTextContent('特种毛');
    expect(screen.getAllByRole('button', { name: /开始执行/ })[0]).toBeEnabled();
  });
});
