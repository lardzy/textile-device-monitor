import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  useNodesInitialized,
  useStore,
  useUpdateNodeInternals,
} from '@xyflow/react';
import {
  CheckCircleFilled,
  ClockCircleFilled,
  CloseCircleFilled,
  LoadingOutlined,
  PauseCircleFilled,
} from '@ant-design/icons';

const STATUS_META = {
  pending: { label: '等待', icon: <ClockCircleFilled /> },
  ready: { label: '待执行', icon: <ClockCircleFilled /> },
  queued: { label: '已排队', icon: <ClockCircleFilled /> },
  running: { label: '执行中', icon: <LoadingOutlined spin /> },
  waiting_human: { label: '等待人工处理', icon: <PauseCircleFilled /> },
  waiting_external: { label: '等待旧系统处理', icon: <PauseCircleFilled /> },
  paused: { label: '已暂停', icon: <PauseCircleFilled /> },
  succeeded: { label: '已完成', icon: <CheckCircleFilled /> },
  completed: { label: '已完成', icon: <CheckCircleFilled /> },
  failed: { label: '失败', icon: <CloseCircleFilled /> },
  skipped: { label: '已跳过', icon: <CheckCircleFilled /> },
};

const ExecutionNode = memo(({ id, data, selected }) => {
  const tone = data.tone || 'default';
  const status = STATUS_META[data.status];
  const isStart = data.nodeType === 'core.start';
  const isEnd = data.nodeType === 'core.end';
  const inputPorts = data.ports?.inputs ?? (isStart ? [] : [{}]);
  const outputPorts = data.ports?.outputs ?? (isEnd ? [] : [{}]);
  const updateNodeInternals = useUpdateNodeInternals();
  const portSignature = JSON.stringify([inputPorts.map(port => port.id ?? null), outputPorts.map(port => port.id ?? null)]);
  const previousPorts = useRef(portSignature);

  useEffect(() => {
    if (previousPorts.current !== portSignature) {
      previousPorts.current = portSignature;
      // Ports can change without resizing the node, so ResizeObserver alone
      // cannot refresh the cached handle bounds after a JSON edit.
      updateNodeInternals(id);
    }
  }, [id, portSignature, updateNodeInternals]);

  return (
    <div
      className={[
        'execution-flow-node',
        `execution-flow-node--${tone}`,
        data.status ? `execution-flow-node--status-${data.status}` : '',
        data.disabled ? 'is-parked' : '',
        selected ? 'is-selected' : '',
      ].filter(Boolean).join(' ')}
      title={data.message || data.description}
    >
      {inputPorts.map((port, index) => (
        <Handle
          key={port.id || 'in'}
          id={port.id}
          title={port.id}
          type="target"
          position={Position.Left}
          className="execution-flow-node__handle"
          style={{ top: `${100 * (index + 1) / (inputPorts.length + 1)}%` }}
        />
      ))}
      <div className="execution-flow-node__type">{data.category || '节点'}</div>
      <strong>{data.label || data.nodeType}</strong>
      {data.disabled && (
        <span className="execution-flow-node__parked-label">已停放 · 发布时不执行</span>
      )}
      <span className="execution-flow-node__description">
        {data.message || data.description || '执行节点'}
      </span>
      {status && (
        <span className={`execution-flow-node__status is-${data.status}`}>
          {status.icon}
          {status.label}
        </span>
      )}
      {outputPorts.map((port, index) => (
        <Handle
          key={port.id || 'out'}
          id={port.id}
          title={port.id}
          type="source"
          position={Position.Right}
          className="execution-flow-node__handle"
          style={{ top: `${100 * (index + 1) / (outputPorts.length + 1)}%` }}
        />
      ))}
    </div>
  );
});

ExecutionNode.displayName = 'ExecutionNode';

const nodeTypes = {
  executionNode: ExecutionNode,
};

function CanvasFocus({ flowInstance, focusNodeKey }) {
  const nodesInitialized = useNodesInitialized();
  const containerReady = useStore(state => state.width > 0 && state.height > 0);
  const lastFocusedKey = useRef(null);

  useEffect(() => {
    const nodeIds = JSON.parse(focusNodeKey);
    if (!nodeIds.length) {
      lastFocusedKey.current = null;
      return;
    }
    if (!flowInstance || !nodesInitialized || !containerReady || lastFocusedKey.current === focusNodeKey) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      lastFocusedKey.current = focusNodeKey;
      void flowInstance.fitView({
        nodes: nodeIds.map(id => ({ id })),
        padding: 0.35,
        maxZoom: 1.2,
        duration: 420,
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [containerReady, flowInstance, focusNodeKey, nodesInitialized]);

  return null;
}

export default function WorkflowCanvas({
  nodes,
  edges,
  readonly = false,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onNodeClick,
  onEdgeClick,
  onPaneClick,
  onDrop,
  onDragOver,
  onInit,
  onDeleteSelection,
  onUndo,
  onRedo,
  onNodeDragStop,
  onReconnect,
  onReconnectStart,
  onReconnectEnd,
  isValidConnection,
  defaultViewport,
  fitView = false,
  focusNodeIds = [],
  children,
}) {
  const [flowInstance, setFlowInstance] = useState(null);
  const [measurements, setMeasurements] = useState(() => new Map());
  const nodeIds = useMemo(() => new Set(nodes.map(node => node.id)), [nodes]);
  // Controlled node updates otherwise discard React Flow's measurements and
  // handle bounds. Keep these presentation details out of the workflow JSON.
  const measuredNodes = useMemo(() => nodes.map(node => {
    const measured = measurements.get(node.id);
    if (!measured || (node.measured?.width === measured.width && node.measured?.height === measured.height)) {
      return node;
    }
    return { ...node, measured };
  }), [measurements, nodes]);
  const handleNodesChange = useCallback((changes) => {
    const resized = changes.filter(change => (
      change.type === 'dimensions' && nodeIds.has(change.id)
      && Number.isFinite(change.dimensions?.width) && change.dimensions.width > 0
      && Number.isFinite(change.dimensions?.height) && change.dimensions.height > 0
    ));
    if (resized.length) {
      setMeasurements(current => {
        let next = current;
        resized.forEach(({ id, dimensions }) => {
          const previous = current.get(id);
          if (previous?.width !== dimensions.width || previous?.height !== dimensions.height) {
            if (next === current) next = new Map(current);
            next.set(id, { width: dimensions.width, height: dimensions.height });
          }
        });
        return next;
      });
    }
    if (!readonly) onNodesChange?.(changes);
  }, [nodeIds, onNodesChange, readonly]);

  useEffect(() => {
    setMeasurements(current => (
      [...current.keys()].every(id => nodeIds.has(id))
        ? current
        : new Map([...current].filter(([id]) => nodeIds.has(id)))
    ));
  }, [nodeIds]);

  const focusNodeKey = useMemo(() => JSON.stringify(
    [...new Set(
      focusNodeIds
        .filter(Boolean)
        .map(String),
    )].sort(),
  ), [focusNodeIds]);
  const handleInit = useCallback((instance) => {
    setFlowInstance(instance);
    onInit?.(instance);
  }, [onInit]);

  return (
    <div style={{ height: '100%' }} tabIndex={readonly ? -1 : 0} aria-label="流程画布" onPointerDownCapture={event => {
      if (!readonly && !event.target.closest('input,textarea,select,[contenteditable="true"]')) event.currentTarget.focus({ preventScroll: true });
    }} onKeyDown={event => {
      if (readonly || event.target.closest('input,textarea,select,[contenteditable="true"]')) return;
      if ((event.key === 'Delete' || event.key === 'Backspace') && onDeleteSelection) { event.preventDefault(); onDeleteSelection(); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z' && onUndo) { event.preventDefault(); event.shiftKey ? onRedo?.() : onUndo(); }
    }}><ReactFlow
      nodes={measuredNodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={handleNodesChange}
      onEdgesChange={readonly ? undefined : onEdgesChange}
      onConnect={readonly ? undefined : onConnect}
      onReconnect={readonly ? undefined : onReconnect}
      onReconnectStart={readonly ? undefined : onReconnectStart}
      onReconnectEnd={readonly ? undefined : onReconnectEnd}
      isValidConnection={isValidConnection}
      onNodeDragStop={onNodeDragStop}
      onNodeClick={onNodeClick}
      onEdgeClick={onEdgeClick}
      onPaneClick={onPaneClick}
      onDrop={readonly ? undefined : onDrop}
      onDragOver={readonly ? undefined : onDragOver}
      onInit={handleInit}
      defaultViewport={defaultViewport}
      nodesDraggable={!readonly}
      nodesConnectable={!readonly}
      elementsSelectable
      selectionOnDrag={!readonly && Boolean(onDeleteSelection)}
      panOnDrag={onDeleteSelection ? [1, 2] : true}
      multiSelectionKeyCode={['Shift', 'Meta', 'Control']}
      deleteKeyCode={null}
      fitView={fitView}
      minZoom={0.25}
      maxZoom={1.8}
      proOptions={{ hideAttribution: true }}
      className={readonly ? 'execution-flow is-readonly' : 'execution-flow'}
    >
      <CanvasFocus flowInstance={flowInstance} focusNodeKey={focusNodeKey} />
      <Background color="#d8e0eb" gap={20} size={1} />
      <Controls showInteractive={!readonly} />
      <MiniMap
        pannable
        zoomable
        nodeColor={node => ({
          red: '#ef6b73',
          orange: '#f3a94f',
          green: '#45b97c',
          purple: '#8568d7',
          cyan: '#34a6b8',
        }[node.data?.tone] || '#5b7cfa')}
      />
      {children}
    </ReactFlow></div>
  );
}
