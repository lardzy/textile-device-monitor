import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import BusinessProfiles from './BusinessProfiles';
import documentFixture from '../../../../backend/app/execution/v2/resources/workflows/fiber-microscopy-v2.json';
import { queryExecutionConnector } from '../../api/execution';
import { testPythonNodeV2 } from '../../api/executionV2';

vi.mock('../../api/execution', () => ({ queryExecutionConnector: vi.fn() }));
vi.mock('../../api/executionV2', () => ({ testPythonNodeV2: vi.fn() }));
vi.mock('./TemplatePicker', () => ({ default: () => <span>选择模板</span> }));
let current;
function Harness() {
  const [document, setDocument] = useState(structuredClone(documentFixture));
  current = document;
  return <BusinessProfiles document={document} onChange={update => setDocument(value => update(structuredClone(value)))} onTemplateSelect={vi.fn()} />;
}
const profiles = () => current.definition.nodes.find(node => node.id === 'profiles').input_mapping.profiles;
beforeEach(() => { vi.clearAllMocks(); });
describe('business profiles', () => {
  it('edits numeric-looking codes as text, copies independent settings, and preserves unrelated graph data', () => {
    render(<Harness />);
    const before = structuredClone(current.definition.nodes.find(node => node.id === 'original'));
    fireEvent.change(screen.getByRole('textbox', { name: '方案 matches 1 project_number' }), { target: { value: '5103.05' } });
    expect(profiles()[0].matches[0].project_number).toBe('5103.05');
    fireEvent.click(screen.getByRole('button', { name: '复制方案' }));
    expect(profiles()).toHaveLength(3);
    expect(profiles()[2].enabled).toBe(false);
    fireEvent.change(screen.getByRole('textbox', { name: '方案 name' }), { target: { value: '独立副本' } });
    expect(profiles()[0].name).not.toBe('独立副本');
    expect(profiles()[2].name).toBe('独立副本');
    expect(current.definition.nodes.find(node => node.id === 'original')).toEqual(before);
  });
  it('previews with the current embedded Python and current profiles without starting a run', async () => {
    queryExecutionConnector.mockResolvedValue({ result: { refresh_status: 'ready', snapshot: { projects: [] } } });
    testPythonNodeV2.mockResolvedValue({ passed: true, output: { projects: [{ id: 'p', label: '按客户要求' }] } });
    render(<Harness />);
    fireEvent.change(screen.getByRole('textbox', { name: '方案 name' }), { target: { value: '最新编辑' } });
    fireEvent.change(screen.getByRole('textbox', { name: '预览检验编号' }), { target: { value: '260221991' } });
    fireEvent.click(screen.getByRole('button', { name: '预览匹配' }));
    await screen.findByText('唯一匹配，运行时自动采用');
    expect(testPythonNodeV2.mock.calls[0][0].inputs.profiles[0].name).toBe('最新编辑');
    expect(queryExecutionConnector.mock.calls[0][0].input).toEqual({ inspection_number: '260221991', refresh: true });
    fireEvent.change(screen.getByRole('textbox', { name: '方案 name' }), { target: { value: '再次修改' } });
    await waitFor(() => expect(screen.queryByText('唯一匹配，运行时自动采用')).not.toBeInTheDocument());
  });
});
