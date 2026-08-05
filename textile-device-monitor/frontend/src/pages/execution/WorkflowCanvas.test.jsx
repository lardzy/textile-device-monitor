import { act, render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import WorkflowCanvas from './WorkflowCanvas';

const flowHarness = vi.hoisted(() => ({
  fitView: vi.fn(() => Promise.resolve(true)),
}));

vi.mock('@xyflow/react', async () => {
  const React = await import('react');
  return {
    Background: () => null,
    Controls: () => null,
    Handle: () => null,
    MiniMap: () => null,
    Position: { Left: 'left', Right: 'right' },
    ReactFlow: function MockReactFlow({ children, onInit }) {
      React.useEffect(() => {
        onInit?.({ fitView: flowHarness.fitView });
      }, [onInit]);
      return React.createElement('div', { 'data-testid': 'react-flow' }, children);
    },
  };
});

const node = (id, message = '') => ({
  id,
  type: 'executionNode',
  position: { x: 0, y: 0 },
  data: {
    nodeType: 'result.aggregate',
    label: id,
    message,
  },
});

describe('WorkflowCanvas viewport focus', () => {
  beforeEach(() => {
    flowHarness.fitView.mockClear();
  });

  it('首次聚焦、同一节点刷新不重复聚焦，并在节点切换时平滑跟随', async () => {
    const { rerender } = render(
      <WorkflowCanvas
        nodes={[node('node-a')]}
        edges={[]}
        readonly
        focusNodeIds={['node-a']}
      />,
    );

    await waitFor(() => expect(flowHarness.fitView).toHaveBeenCalledTimes(1));
    expect(flowHarness.fitView).toHaveBeenLastCalledWith({
      nodes: [{ id: 'node-a' }],
      padding: 0.35,
      maxZoom: 1.2,
      duration: 420,
    });

    rerender(
      <WorkflowCanvas
        nodes={[node('node-a', '状态消息已刷新')]}
        edges={[]}
        readonly
        focusNodeIds={['node-a']}
      />,
    );
    await act(async () => {});
    expect(flowHarness.fitView).toHaveBeenCalledTimes(1);

    rerender(
      <WorkflowCanvas
        nodes={[node('node-a'), node('node-b')]}
        edges={[]}
        readonly
        focusNodeIds={['node-b']}
      />,
    );
    await waitFor(() => expect(flowHarness.fitView).toHaveBeenCalledTimes(2));
    expect(flowHarness.fitView).toHaveBeenLastCalledWith(expect.objectContaining({
      nodes: [{ id: 'node-b' }],
    }));
  });

  it('并行活动节点共同进入视口且顺序变化不会触发重复聚焦', async () => {
    const { rerender } = render(
      <WorkflowCanvas
        nodes={[node('node-a'), node('node-b')]}
        edges={[]}
        readonly
        focusNodeIds={['node-b', 'node-a']}
      />,
    );

    await waitFor(() => expect(flowHarness.fitView).toHaveBeenCalledTimes(1));
    expect(flowHarness.fitView).toHaveBeenLastCalledWith(expect.objectContaining({
      nodes: [{ id: 'node-a' }, { id: 'node-b' }],
    }));

    rerender(
      <WorkflowCanvas
        nodes={[node('node-a'), node('node-b')]}
        edges={[]}
        readonly
        focusNodeIds={['node-a', 'node-b']}
      />,
    );
    await act(async () => {});
    expect(flowHarness.fitView).toHaveBeenCalledTimes(1);
  });

  it('没有聚焦节点时保留用户当前视口', async () => {
    render(
      <WorkflowCanvas
        nodes={[node('node-a')]}
        edges={[]}
        readonly
        focusNodeIds={[]}
      />,
    );

    await act(async () => {});
    expect(flowHarness.fitView).not.toHaveBeenCalled();
  });
});
