import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../../tests/testServer';
import { ExecutionAuthProvider } from './ExecutionAuthContext';
import ExecutionWorkflowDesigner from './ExecutionWorkflowDesigner';

vi.mock('./WorkflowCanvas', () => ({
  default: ({
    nodes,
    onNodeClick,
    onDrop,
    onDragOver,
    onInit,
  }) => (
    <div
      data-testid="workflow-canvas"
      onMouseEnter={() => onInit?.({
        screenToFlowPosition: point => point,
        getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
      })}
      onDrop={onDrop}
      onDragOver={onDragOver}
    >
      {nodes.map(node => (
        <button
          key={node.id}
          type="button"
          onClick={() => onNodeClick?.({}, node)}
        >
          {node.data.label}
        </button>
      ))}
    </div>
  ),
}));

const draftDefinition = {
  schema_version: '1.0',
  metadata: { name: '测试流程' },
  input_schema: {
    type: 'object',
    required: ['inspection_number', 'sample_count'],
    properties: {
      inspection_number: { type: 'string', title: '检验编号' },
      sample_count: { type: 'integer', title: '样品数量', minimum: 1 },
      remark: { type: 'string', title: '备注' },
    },
  },
  global_schema: {
    type: 'object',
    required: ['reviewer'],
    properties: {
      reviewer: { type: 'string', title: '复核人' },
    },
  },
  root_slots: [],
  credential_slots: [],
  nodes: [
    {
      id: 'start',
      type: 'core.start',
      type_version: 1,
      name: '开始',
      config: {},
      input_mapping: {},
      ui: { x: 0, y: 0 },
    },
    {
      id: 'aggregate',
      type: 'result.aggregate',
      type_version: 1,
      name: '结果汇总',
      config: {},
      input_mapping: {},
      ui: { x: 200, y: 0 },
    },
    {
      id: 'end',
      type: 'core.end',
      type_version: 1,
      name: '结束',
      config: {},
      input_mapping: {},
      ui: { x: 400, y: 0 },
    },
  ],
  edges: [
    { id: 'a', source: 'start', target: 'aggregate', join_policy: 'all' },
    { id: 'b', source: 'aggregate', target: 'end', join_policy: 'all' },
  ],
  viewport: { x: 0, y: 0, zoom: 1 },
};

const registeredNodeTypes = [
  {
    type: 'core.start',
    version: 1,
    name: '开始',
    category: '基础',
    description: '流程入口',
    config_schema: { type: 'object', properties: {} },
  },
  {
    type: 'result.aggregate',
    version: 1,
    name: '结果汇总',
    category: '基础',
    description: '汇总上游输出',
    config_schema: { type: 'object', properties: {} },
  },
  {
    type: 'core.end',
    version: 1,
    name: '结束',
    category: '基础',
    description: '流程出口',
    config_schema: { type: 'object', properties: {} },
  },
];

const renderDesigner = () => render(
  <MemoryRouter
    initialEntries={['/execution/admin/workflows/wf-1']}
    future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
  >
    <ExecutionAuthProvider>
      <Routes>
        <Route
          path="/execution/admin/workflows/:workflowId"
          element={<ExecutionWorkflowDesigner />}
        />
        <Route path="/execution/runs/:runId" element={<div>测试工作台</div>} />
        <Route path="/execution/admin" element={<div>流程管理页</div>} />
      </Routes>
    </ExecutionAuthProvider>
  </MemoryRouter>,
);

describe('ExecutionWorkflowDesigner', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/execution/v1/auth/me', () => HttpResponse.json({
        user: {
          id: 'admin-1',
          username: 'admin',
          display_name: '管理员',
          role: 'admin',
          permissions: ['workflow.design', 'workflow.publish', 'workflow.run'],
        },
      })),
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'csrf-test' })),
      http.get('/api/execution/v1/workflows/wf-1', () => HttpResponse.json({
        workflow: {
          id: 'wf-1',
          name: '测试流程',
          description: '设计器测试流程',
          draft_revision: 3,
          draft_definition: draftDefinition,
        },
      })),
      http.get('/api/execution/v1/node-types', () => HttpResponse.json({
        items: registeredNodeTypes,
      })),
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
      http.post('/api/execution/v1/workflows/wf-1/validate', () =>
        HttpResponse.json({ issues: [] })),
    );
  });

  it('测试运行对话框收集草稿中的完整输入和全局变量', async () => {
    let requestBody;
    server.use(
      http.post('/api/execution/v1/workflows/wf-1/test', async ({ request }) => {
        requestBody = await request.json();
        return HttpResponse.json({ run: { id: 'test-run-1' } });
      }),
    );
    const user = userEvent.setup();
    renderDesigner();

    const testRunLabel = await screen.findByText('测试运行');
    await user.click(testRunLabel.closest('button'));
    const dialog = await screen.findByRole('dialog', { name: '创建测试运行' });
    expect(within(dialog).getByRole('spinbutton', { name: '样品数量' })).toBeEnabled();
    expect(within(dialog).getByRole('textbox', { name: '备注' })).toBeEnabled();
    expect(within(dialog).getByRole('textbox', { name: '复核人' })).toBeEnabled();

    await user.type(within(dialog).getByRole('spinbutton', { name: '样品数量' }), '2');
    await user.type(within(dialog).getByRole('textbox', { name: '备注' }), '测试备注');
    await user.type(within(dialog).getByRole('textbox', { name: '复核人' }), '王工');
    await user.click(within(dialog).getByRole('button', { name: '开始测试' }));

    expect(await screen.findByText('测试工作台')).toBeInTheDocument();
    expect(requestBody.input_data).toMatchObject({
      sample_count: 2,
      remark: '测试备注',
    });
    expect(requestBody.global_data).toEqual({ reviewer: '王工' });
  });

  it('节点可以停放并恢复，草稿序列化保留 disabled 标记', async () => {
    const savedDefinitions = [];
    server.use(
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        savedDefinitions.push(body.definition);
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
    );
    const user = userEvent.setup();
    renderDesigner();

    await user.click(await screen.findByRole('button', { name: '结果汇总' }));
    const stateSwitch = screen.getByRole('switch');
    expect(stateSwitch).toBeChecked();
    await user.click(stateSwitch);
    expect(stateSwitch).not.toBeChecked();

    await waitFor(() => {
      expect(savedDefinitions.some(definition => (
        definition.nodes.find(node => node.id === 'aggregate')?.disabled === true
      ))).toBe(true);
    }, { timeout: 3000 });
  });

  it('可视化编辑数据根和凭据槽位，自动保存会保留结构化定义', async () => {
    const savedDefinitions = [];
    server.use(
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        savedDefinitions.push(body.definition);
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
    );
    const user = userEvent.setup();
    renderDesigner();

    const slotsEditor = await screen.findByRole('region', { name: '资源槽位' });
    const addRootButton = within(slotsEditor).getByRole('button', { name: '新增数据根槽位' });
    fireEvent.click(addRootButton);
    expect(addRootButton).toHaveAttribute('aria-expanded', 'true');
    const rootDialog = (await screen.findByText('新增数据根槽位')).closest('.ant-modal-content');
    await user.type(within(rootDialog).getByRole('textbox', { name: /槽位名称/ }), 'publish_output');
    await user.type(within(rootDialog).getByRole('textbox', { name: /数据根标识/ }), 'execution_publish');
    await user.click(within(rootDialog).getByRole('combobox', { name: '访问权限' }));
    await user.click(await screen.findByText('发布', { selector: '.ant-select-item-option-content' }));
    await user.click(within(rootDialog).getByRole('button', { name: /添.*加/ }));

    const rootItem = within(slotsEditor).getByText('publish_output').closest('.ant-list-item');
    await user.click(within(rootItem).getByRole('button', { name: '编辑' }));
    const editRootDialog = (await screen.findByText('编辑数据根槽位')).closest('.ant-modal-content');
    const rootNameInput = within(editRootDialog).getByRole('textbox', { name: /槽位名称/ });
    await user.clear(rootNameInput);
    await user.type(rootNameInput, 'published_result');
    await user.click(within(editRootDialog).getByRole('button', { name: /保.*存/ }));

    await user.click(within(slotsEditor).getByText('系统凭据 0'));
    await user.click(within(slotsEditor).getByRole('button', { name: '新增凭据槽位' }));
    const credentialDialog = (await screen.findByText('新增凭据槽位')).closest('.ant-modal-content');
    await user.type(
      within(credentialDialog).getByRole('textbox', { name: /槽位名称/ }),
      'inspection_account',
    );
    await user.click(within(credentialDialog).getByRole('combobox', { name: '外部系统' }));
    await user.click(await screen.findByText('新检务系统', { selector: '.ant-select-item-option-content' }));
    await user.click(within(credentialDialog).getByRole('combobox', { name: '运行时要求' }));
    await user.click(await screen.findByText(
      '选填：未绑定时允许继续',
      { selector: '.ant-select-item-option-content' },
    ));
    await user.click(within(credentialDialog).getByRole('button', { name: /添.*加/ }));

    await waitFor(() => {
      expect(savedDefinitions.some(definition => (
        definition.root_slots?.[0]?.name === 'published_result'
        && definition.root_slots[0].root_id === 'execution_publish'
        && definition.root_slots[0].access === 'publish'
        && definition.credential_slots?.[0]?.name === 'inspection_account'
        && definition.credential_slots[0].system_key === 'new_inspection'
        && definition.credential_slots[0].required === false
      ))).toBe(true);
    }, { timeout: 4000 });
  });

  it('移除资源槽位后会从自动保存定义中删除', async () => {
    const definitionWithSlots = {
      ...draftDefinition,
      root_slots: [{
        name: 'source',
        root_id: 'special_wool_records',
        access: 'read',
      }],
    };
    const savedDefinitions = [];
    server.use(
      http.get('/api/execution/v1/workflows/wf-1', () => HttpResponse.json({
        workflow: {
          id: 'wf-1',
          name: '测试流程',
          description: '设计器测试流程',
          draft_revision: 3,
          draft_definition: definitionWithSlots,
        },
      })),
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        savedDefinitions.push(body.definition);
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
    );
    const user = userEvent.setup();
    renderDesigner();

    const slotsEditor = await screen.findByRole('region', { name: '资源槽位' });
    const rootItem = within(slotsEditor).getByText('source').closest('.ant-list-item');
    fireEvent.click(within(rootItem).getByRole('button', { name: '移除' }));
    const confirmation = (await screen.findByText(
      '引用此槽位的节点可能无法通过流程校验。',
    )).closest('.ant-popover-inner');
    await user.click(within(confirmation).getAllByRole('button').at(-1));

    await waitFor(() => {
      expect(savedDefinitions.some(definition => definition.root_slots?.length === 0)).toBe(true);
    }, { timeout: 3000 });
  });

  it('节点库由服务端注册表生成，拖入后保留服务端节点版本', async () => {
    const savedDefinitions = [];
    server.use(
      http.get('/api/execution/v1/node-types', () => HttpResponse.json({
        items: [
          ...registeredNodeTypes,
          {
            type: 'custom.server_node',
            version: 7,
            name: '服务端专用节点',
            category: '扩展能力',
            description: '仅存在于本次服务端注册表',
            config_schema: {
              type: 'object',
              properties: {
                mode: { type: 'string' },
              },
              required: ['mode'],
            },
          },
        ],
      })),
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        savedDefinitions.push(body.definition);
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
    );
    renderDesigner();

    const paletteNode = await screen.findByRole('button', {
      name: /服务端专用节点/,
    });
    expect(paletteNode).toHaveAttribute('title', 'custom.server_node@7');

    const transferred = new Map();
    const dataTransfer = {
      setData: (key, value) => transferred.set(key, value),
      getData: key => transferred.get(key) || '',
      effectAllowed: '',
      dropEffect: '',
    };
    fireEvent.dragStart(paletteNode, { dataTransfer });
    expect(transferred.get('application/execution-node')).toBe('custom.server_node');
    expect(transferred.get('application/execution-node-version')).toBe('7');

    const canvas = screen.getByTestId('workflow-canvas');
    fireEvent.mouseEnter(canvas);
    fireEvent.drop(canvas, { dataTransfer, clientX: 180, clientY: 120 });

    expect(await within(canvas).findByRole('button', {
      name: '服务端专用节点',
    })).toBeInTheDocument();
    expect(screen.getByText('服务端参数约束')).toBeInTheDocument();
    expect(screen.getByText('mode *')).toBeInTheDocument();
    await waitFor(() => {
      expect(savedDefinitions.some(definition => definition.nodes?.some(node => (
        node.type === 'custom.server_node' && node.type_version === 7
      )))).toBe(true);
    }, { timeout: 4000 });
  });

  it('节点注册表加载失败时保留草稿展示并禁用新增节点', async () => {
    server.use(
      http.get('/api/execution/v1/node-types', () => HttpResponse.json({
        code: 'node_registry_unavailable',
        message: '节点注册表暂不可用',
      }, { status: 503 })),
    );
    renderDesigner();

    expect(await screen.findByText('节点类型加载失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重新加载' })).toBeEnabled();
    const canvas = screen.getByTestId('workflow-canvas');
    expect(within(canvas).getByRole('button', { name: '开始' })).toBeInTheDocument();
    expect(within(canvas).getByRole('button', { name: '结束' })).toBeInTheDocument();
    expect(document.querySelectorAll('.execution-node-palette__item')).toHaveLength(0);
  });

  it('无效的局部 JSON 会阻止保存和发布，修复后发布包含最新配置', async () => {
    const savedDefinitions = [];
    const publishRequests = [];
    server.use(
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        savedDefinitions.push(body.definition);
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
      http.post('/api/execution/v1/workflows/wf-1/publish', async ({ request }) => {
        publishRequests.push(await request.json());
        return HttpResponse.json({ version: 1 });
      }),
    );
    const user = userEvent.setup();
    renderDesigner();

    const canvas = await screen.findByTestId('workflow-canvas');
    await user.click(within(canvas).getByRole('button', { name: '结果汇总' }));
    const configEditor = screen.getByRole('textbox', { name: '节点参数 JSON' });
    fireEvent.change(configEditor, { target: { value: '{"mode":' } });

    expect(await screen.findByText('当前节点或连线配置尚未应用')).toBeInTheDocument();
    expect(screen.getByText('配置格式待修复')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /保存/ })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '发布版本' }));
    expect(publishRequests).toHaveLength(0);

    fireEvent.change(configEditor, {
      target: { value: '{"mode":"verified-latest"}' },
    });
    await waitFor(() => {
      expect(screen.queryByText('当前节点或连线配置尚未应用')).not.toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: '发布版本' }));

    await waitFor(() => expect(publishRequests).toHaveLength(1));
    expect(savedDefinitions.at(-1).nodes.find(node => node.id === 'aggregate')?.config)
      .toEqual({ mode: 'verified-latest' });
  });

  it('点击设计器返回按钮时先冲刷尚未到自动保存时间的草稿', async () => {
    const savedDefinitions = [];
    server.use(
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        savedDefinitions.push(body.definition);
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
    );
    const user = userEvent.setup();
    renderDesigner();

    const canvas = await screen.findByTestId('workflow-canvas');
    await user.click(within(canvas).getByRole('button', { name: '结果汇总' }));
    const nameInput = screen.getByRole('textbox', { name: '节点名称' });
    await user.clear(nameInput);
    await user.type(nameInput, '最终结果汇总');
    await user.click(screen.getByRole('button', { name: '← 流程管理' }));

    expect(await screen.findByText('流程管理页')).toBeInTheDocument();
    expect(savedDefinitions.at(-1).nodes.find(node => node.id === 'aggregate')?.name)
      .toBe('最终结果汇总');
  });

  it('组件因站内导航卸载时会尽力冲刷有效的未保存草稿', async () => {
    const savedDefinitions = [];
    server.use(
      http.put('/api/execution/v1/workflows/wf-1/draft', async ({ request }) => {
        const body = await request.json();
        savedDefinitions.push(body.definition);
        return HttpResponse.json({ revision: body.revision + 1 });
      }),
    );
    const user = userEvent.setup();
    const view = renderDesigner();

    const canvas = await screen.findByTestId('workflow-canvas');
    await user.click(within(canvas).getByRole('button', { name: '结果汇总' }));
    const nameInput = screen.getByRole('textbox', { name: '节点名称' });
    await user.clear(nameInput);
    await user.type(nameInput, '卸载前保存的结果汇总');
    view.unmount();

    await waitFor(() => expect(savedDefinitions).toHaveLength(1));
    expect(savedDefinitions[0].nodes.find(node => node.id === 'aggregate')?.name)
      .toBe('卸载前保存的结果汇总');
  });

  it('release_v2 管理的流程不进入 v1 画布并只显示转交入口', async () => {
    server.use(
      http.get('/api/execution/v1/workflows/wf-1', () => HttpResponse.json({
        workflow: {
          id: 'wf-1',
          name: 'Release 管理流程',
          management_mode: 'release_v2',
          active_release_id: 'release-1',
          draft_definition: {
            format: 'textile-workflow-release',
            format_version: '2.0',
            definition: { schema_version: '2.0' },
          },
        },
      })),
    );

    renderDesigner();

    expect(await screen.findByText('该流程不能在 v1 草稿设计器中编辑')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '前往 Workflow Release 管理' })).toBeEnabled();
    expect(screen.queryByTestId('workflow-canvas')).not.toBeInTheDocument();
  });
});
