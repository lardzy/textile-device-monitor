import { act, render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { adoptUserNodes } from '@xyflow/system';
import WorkflowCanvas from './WorkflowCanvas';

const flowHarness = vi.hoisted(() => ({
  fitView: vi.fn(() => Promise.resolve(true)),
  initialized: true,
  width: 800,
  height: 600,
  props: null,
  renderNodes: false,
  updateNodeInternals: vi.fn(),
}));

vi.mock('@xyflow/react', async () => {
  const React = await import('react');
  return {
    Background: () => null,
    Controls: () => null,
    Handle: () => null,
    MiniMap: () => null,
    Position: { Left: 'left', Right: 'right' },
    useNodesInitialized: () => flowHarness.initialized,
    useUpdateNodeInternals: () => flowHarness.updateNodeInternals,
    useStore: selector => selector({ width: flowHarness.width, height: flowHarness.height }),
    ReactFlow: function MockReactFlow(props) {
      const { children, onInit } = props;
      flowHarness.props = props;
      React.useEffect(() => {
        onInit?.({ fitView: flowHarness.fitView });
      }, [onInit]);
      return React.createElement('div', { 'data-testid': 'react-flow' },
        flowHarness.renderNodes && props.nodes.map(node => React.createElement(props.nodeTypes[node.type], { ...node, key: node.id })), children);
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
    flowHarness.initialized = true;
    flowHarness.width = 800;
    flowHarness.height = 600;
    flowHarness.props = null;
    flowHarness.renderNodes = false;
    flowHarness.updateNodeInternals.mockClear();
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

  it.each([true, false])('保留尺寸和连接点，状态刷新不会让已测量节点重新隐藏（readonly=%s）', async (readonly) => {
    const incoming = [node('node-a')];
    const onNodesChange = vi.fn();
    const { rerender } = render(<WorkflowCanvas nodes={incoming} edges={[]} readonly={readonly} onNodesChange={onNodesChange} />);
    await act(async () => {
      flowHarness.props.onNodesChange?.([{ type: 'dimensions', id: 'node-a', dimensions: { width: 240, height: 120 } }]);
    });
    const lookup = new Map();
    const parents = new Map();
    adoptUserNodes(flowHarness.props.nodes, lookup, parents);
    lookup.get('node-a').internals.handleBounds = { source: [], target: [] };
    rerender(<WorkflowCanvas nodes={[node('node-a', '刷新后的消息')]} edges={[]} readonly={readonly} onNodesChange={onNodesChange} />);
    const result = adoptUserNodes(flowHarness.props.nodes, lookup, parents);
    expect(result.nodesInitialized).toBe(true);
    expect(lookup.get('node-a').measured).toEqual({ width: 240, height: 120 });
    expect(lookup.get('node-a').internals.handleBounds).toEqual({ source: [], target: [] });
    expect(flowHarness.props.nodes[0].data.message).toBe('刷新后的消息');
    expect(incoming[0].measured).toBeUndefined();
    expect(onNodesChange).toHaveBeenCalledTimes(readonly ? 0 : 1);
  });

  it('节点测量与容器尺寸就绪后才聚焦，恢复测量不会重复抢占视口', async () => {
    flowHarness.initialized = false;
    const props = { nodes: [node('node-a')], edges: [], readonly: true, focusNodeIds: ['node-a'] };
    const { rerender } = render(<WorkflowCanvas {...props} />);
    await act(async () => {});
    expect(flowHarness.fitView).not.toHaveBeenCalled();
    flowHarness.initialized = true;
    flowHarness.width = 0;
    rerender(<WorkflowCanvas {...props} />);
    await act(async () => {});
    expect(flowHarness.fitView).not.toHaveBeenCalled();
    flowHarness.width = 800;
    rerender(<WorkflowCanvas {...props} />);
    await waitFor(() => expect(flowHarness.fitView).toHaveBeenCalledTimes(1));
    flowHarness.initialized = false;
    rerender(<WorkflowCanvas {...props} />);
    flowHarness.initialized = true;
    rerender(<WorkflowCanvas {...props} />);
    await act(async () => {});
    expect(flowHarness.fitView).toHaveBeenCalledTimes(1);
  });

  it('节点移除后丢弃旧尺寸，重新添加相同 ID 的节点仍正常测量', async () => {
    const { rerender } = render(<WorkflowCanvas nodes={[node('node-a')]} edges={[]} />);
    await act(async () => {
      flowHarness.props.onNodesChange?.([{ type: 'dimensions', id: 'node-a', dimensions: { width: 240, height: 120 } }]);
    });
    expect(flowHarness.props.nodes[0].measured).toEqual({ width: 240, height: 120 });
    rerender(<WorkflowCanvas nodes={[]} edges={[]} />);
    rerender(<WorkflowCanvas nodes={[node('node-a')]} edges={[]} />);
    expect(flowHarness.props.nodes[0].measured).toBeUndefined();
  });

  it('内容变高时更新尺寸，隐藏容器的零尺寸不覆盖有效测量', async () => {
    render(<WorkflowCanvas nodes={[node('node-a')]} edges={[]} readonly />);
    const resize = dimensions => act(async () => {
      flowHarness.props.onNodesChange([{ type: 'dimensions', id: 'node-a', dimensions }]);
    });
    await resize({ width: 240, height: 120 });
    await resize({ width: 240, height: 180 });
    await resize({ width: 0, height: 0 });
    expect(flowHarness.props.nodes[0].measured).toEqual({ width: 240, height: 180 });
  });

  it('同 ID 节点的端口变更时重新测量连接点，普通状态刷新不重测', async () => {
    flowHarness.renderNodes = true;
    const start = { ...node('step'), data: { label: '步骤', nodeType: 'core.start' } };
    const { rerender } = render(<WorkflowCanvas nodes={[start]} edges={[]} />);
    await act(async () => {
      flowHarness.props.onNodesChange([{ type: 'dimensions', id: 'step', dimensions: { width: 240, height: 120 } }]);
    });
    expect(flowHarness.updateNodeInternals).not.toHaveBeenCalled();
    const end = { ...start, data: { ...start.data, nodeType: 'core.end' } };
    rerender(<WorkflowCanvas nodes={[end]} edges={[]} />);
    expect(flowHarness.updateNodeInternals).toHaveBeenCalledTimes(1);
    expect(flowHarness.updateNodeInternals).toHaveBeenLastCalledWith('step');
    expect(flowHarness.props.nodes[0].measured).toEqual({ width: 240, height: 120 });
    const branch = { ...end, data: { ...end.data, ports: { inputs: [{ id: 'in' }], outputs: [{ id: 'yes' }, { id: 'no' }] } } };
    rerender(<WorkflowCanvas nodes={[branch]} edges={[]} />);
    expect(flowHarness.updateNodeInternals).toHaveBeenCalledTimes(2);
    rerender(<WorkflowCanvas nodes={[{ ...branch, data: { ...branch.data, message: '刷新状态', ports: structuredClone(branch.data.ports) } }]} edges={[]} />);
    expect(flowHarness.updateNodeInternals).toHaveBeenCalledTimes(2);
    rerender(<WorkflowCanvas nodes={[{ ...branch, data: { ...branch.data, ports: { inputs: [{ id: 'in' }], outputs: [{ id: 'no' }, { id: 'yes' }] } } }]} edges={[]} />);
    expect(flowHarness.updateNodeInternals).toHaveBeenCalledTimes(3);
  });

  it('快速切换焦点时取消上一帧的聚焦，卸载后不继续移动视口', async () => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] });
    try {
      const props = { nodes: [node('node-a'), node('node-b')], edges: [], readonly: true };
      const { rerender, unmount } = render(<WorkflowCanvas {...props} focusNodeIds={['node-a']} />);
      rerender(<WorkflowCanvas {...props} focusNodeIds={['node-b']} />);
      await act(async () => vi.advanceTimersByTime(32));
      expect(flowHarness.fitView).toHaveBeenCalledTimes(1);
      expect(flowHarness.fitView).toHaveBeenCalledWith(expect.objectContaining({ nodes: [{ id: 'node-b' }] }));
      rerender(<WorkflowCanvas {...props} focusNodeIds={['node-a']} />);
      unmount();
      await act(async () => vi.advanceTimersByTime(32));
      expect(flowHarness.fitView).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
