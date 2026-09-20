import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ExecutionWorkflowV2Designer from './ExecutionWorkflowV2Designer';
import { applyWorkflowReleaseV2, preflightWorkflowReleaseV2, compileWorkflowDesignerV2, getWorkflowDesignerCatalogV2 } from '../../api/executionV2';

vi.mock('../../api/executionV2', () => ({ applyWorkflowReleaseV2: vi.fn(), preflightWorkflowReleaseV2: vi.fn(), getWorkflowDesignerCatalogV2: vi.fn(), compileWorkflowDesignerV2: vi.fn(), getWorkflowReleaseV2: vi.fn() }));
vi.mock('./ExecutionChrome', () => ({ default: ({ title, actions }) => <header>{title}{actions}</header> }));
vi.mock('./WorkflowCanvas', () => ({ default: () => <div>流程画布</div> }));
const starter = { format: 'textile-workflow-release', release: { slug: 'new-workflow-v2', name: '新工作流', release_version: 1 }, resources: {}, definition: {
  input_schema: { type: 'object', properties: { number: { type: 'string' } } }, output_schema: { type: 'object', properties: { value: { type: 'string' } } },
  nodes: [{ id: 'start', type: 'core.start', type_version: 2, name: '开始', config: {}, input_mapping: {} }, { id: 'end', type: 'core.end', type_version: 2, name: '结束', config: {}, input_mapping: {} }], edges: [],
} };
const Destination = () => <pre data-testid="release">{JSON.stringify(useLocation().state)}</pre>;
const show = () => render(<MemoryRouter initialEntries={['/designer']}><Routes><Route path="/designer" element={<ExecutionWorkflowV2Designer />} /><Route path="/execution/admin/releases/:releaseId" element={<Destination />} /></Routes></MemoryRouter>);
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks();
  preflightWorkflowReleaseV2.mockResolvedValue({ content_valid: true, preflight_token: "test-token" });
  applyWorkflowReleaseV2.mockResolvedValue({ id: "compiled" });
  getWorkflowDesignerCatalogV2.mockResolvedValue({ starter: structuredClone(starter), templates: [], connectors: [], node_specs: starter.definition.nodes.map(node => ({ ...node, config_schema: { type: 'object', properties: {} }, schema_bindings: { input: { source: node.id === 'end' ? 'workflow_output_schema' : 'workflow_input_schema' } } })) });
});
describe('v2 workflow designer', () => {
  it('edits a schema-driven input, saves a browser draft and prepares a release', async () => {
    compileWorkflowDesignerV2.mockImplementation(async document => ({ content_valid: true, document: { ...document, integrity: { digest: 'compiled' } }, issues: [] }));
    show();
    await screen.findByDisplayValue('新工作流');
    fireEvent.click(screen.getAllByRole('button', { name: '调整' })[1]);
    fireEvent.change(screen.getByRole('combobox', { name: '输入 value' }), { target: { value: '$.inputs.number' } });
    await waitFor(() => expect(JSON.parse(localStorage.getItem('execution:v2-designer:new')).document.definition.nodes[1].input_mapping.value).toBe('$.inputs.number'));
    fireEvent.click(screen.getByRole('button', { name: '准备发布' }));
    await screen.findByTestId('release');
    expect(applyWorkflowReleaseV2).toHaveBeenCalledWith('test-token');
    expect(preflightWorkflowReleaseV2.mock.calls[0][0].integrity.digest).toBe('compiled');
    expect(compileWorkflowDesignerV2.mock.calls[0][0].definition.nodes[1].input_mapping).toEqual({ value: '$.inputs.number' });
  });
  it('keeps invalid graphs editable and displays compiler issues', async () => {
    compileWorkflowDesignerV2.mockResolvedValue({ content_valid: false, issues: [{ message: '缺少连接', path: '$.definition.edges' }] });
    show(); await screen.findByDisplayValue('新工作流');
    fireEvent.click(screen.getByRole('button', { name: '准备发布' }));
    expect(await screen.findByText('缺少连接')).toBeInTheDocument();
    expect(screen.getByDisplayValue('新工作流')).toBeInTheDocument();
  });
});
