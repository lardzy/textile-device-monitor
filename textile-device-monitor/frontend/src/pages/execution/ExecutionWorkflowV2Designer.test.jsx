import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ExecutionWorkflowV2Designer from './ExecutionWorkflowV2Designer';
import * as api from '../../api/executionV2';
import { getExecutionCredentials, getExecutionFileRoots } from '../../api/execution';

vi.mock('../../api/executionV2', () => Object.fromEntries(['applyWorkflowReleaseV2', 'preflightWorkflowReleaseV2', 'compileWorkflowDesignerV2', 'getWorkflowDesignerCatalogV2', 'getWorkflowReleaseV2', 'getWorkflowReleasesV2', 'testWorkflowDesignerV2', 'createDesignerDraftV2', 'getDesignerDraftV2', 'saveDesignerDraftV2', 'updateWorkflowReleaseBindingV2', 'preflightStagedWorkflowReleaseV2', 'publishWorkflowReleaseV2', 'testPythonNodeV2'].map(key => [key, vi.fn()])));
vi.mock('../../api/execution', () => ({ getExecutionCredentials: vi.fn(), getExecutionFileRoots: vi.fn() }));
vi.mock('./ExecutionChrome', () => ({ default: ({ title, actions }) => <header>{title}{actions}</header> }));
vi.mock('./WorkflowCanvas', () => ({ default: ({ nodes, onNodeClick, onDeleteSelection }) => <div data-testid="canvas" onKeyDown={event => { if (event.key === 'Delete') onDeleteSelection(); }}>{nodes.map(node => <button key={node.id} onClick={event => onNodeClick(event, node)}>{node.data.label}</button>)}</div> }));
const starter = { format: 'textile-workflow-release', release: { slug: 'new-workflow-v2', name: '新工作流', release_version: 1 }, resources: {}, definition: {
  input_schema: { type: 'object', properties: { number: { type: 'string' } } }, output_schema: { type: 'object', properties: { value: { type: 'string' } } },
  nodes: [{ id: 'start', type: 'core.start', type_version: 2, name: '开始', config: {}, input_mapping: {} }, { id: 'end', type: 'core.end', type_version: 2, name: '结束', config: {}, input_mapping: {} }], edges: [],
} };
const show = () => render(<MemoryRouter initialEntries={['/designer']}><Routes><Route path="/designer" element={<ExecutionWorkflowV2Designer />} /><Route path="/execution/admin/designer/workflows/:workflowId" element={<ExecutionWorkflowV2Designer />} /></Routes></MemoryRouter>);
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks();
  getExecutionCredentials.mockResolvedValue([]); getExecutionFileRoots.mockResolvedValue([]);
  api.getWorkflowDesignerCatalogV2.mockResolvedValue({ starter: structuredClone(starter), templates: [], connectors: [], node_specs: starter.definition.nodes.map(node => ({ ...node, config_schema: { type: 'object', properties: {} }, schema_bindings: { input: { source: node.id === 'end' ? 'workflow_output_schema' : 'workflow_input_schema' } } })) });
  api.createDesignerDraftV2.mockImplementation(async payload => ({ ...payload, workflow_id: 'draft', revision: 1 }));
  api.getWorkflowReleasesV2.mockResolvedValue({ items: [] });
});
describe('v2 workflow designer', () => {
  it('saves incomplete drafts independently of releases, preserves edits on conflict', async () => {
    show(); await screen.findByDisplayValue('新工作流');
    fireEvent.click(screen.getByRole('button', { name: '开始', exact: true }));
    fireEvent.keyDown(screen.getByTestId('canvas'), { key: 'Delete' });
    expect(screen.queryByRole('button', { name: '开始', exact: true })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /撤\s*销/, exact: true }));
    expect(screen.getByRole('button', { name: '开始', exact: true })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /重\s*做/, exact: true }));
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    await waitFor(() => expect(api.createDesignerDraftV2).toHaveBeenCalled());
    expect(api.createDesignerDraftV2.mock.calls[0][0].document.definition.nodes.map(node => node.id)).toEqual(['end']);
    api.saveDesignerDraftV2.mockRejectedValue(new Error('草稿已由其他页面更新'));
    fireEvent.change(screen.getByRole('textbox', { name: '流程名称' }), { target: { value: '保留我的修改' } });
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    await screen.findByText('草稿已由其他页面更新');
    expect(screen.getByDisplayValue('保留我的修改')).toBeInTheDocument();
    expect(api.applyWorkflowReleaseV2).not.toHaveBeenCalled();
  });
  it('preserves numeric-looking string inputs and publishes current edits', async () => {
    api.compileWorkflowDesignerV2.mockResolvedValue({ content_valid: false, issues: [{ message: '缺少连接', path: '$.definition.edges' }] });
    show(); await screen.findByDisplayValue('新工作流');
    fireEvent.click(screen.getByRole('button', { name: '结束', exact: true }));
    fireEvent.change(screen.getByRole('textbox', { name: '输入 value' }), { target: { value: '1000' } });
    fireEvent.click(screen.getByRole('button', { name: /发\s*布/, exact: true }));
    await screen.findByText('缺少连接');
    expect(api.compileWorkflowDesignerV2.mock.calls[0][0].definition.nodes[1].input_mapping.value).toBe('1000');
    expect(screen.getByDisplayValue('1000')).toBeInTheDocument();
  });
});
