import { memo } from 'react';
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
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
  queued: { label: '已排队', icon: <ClockCircleFilled /> },
  running: { label: '执行中', icon: <LoadingOutlined spin /> },
  waiting_human: { label: '等待人工处理', icon: <PauseCircleFilled /> },
  paused: { label: '已暂停', icon: <PauseCircleFilled /> },
  succeeded: { label: '已完成', icon: <CheckCircleFilled /> },
  completed: { label: '已完成', icon: <CheckCircleFilled /> },
  failed: { label: '失败', icon: <CloseCircleFilled /> },
  skipped: { label: '已跳过', icon: <CheckCircleFilled /> },
};

const ExecutionNode = memo(({ data, selected }) => {
  const tone = data.tone || 'default';
  const status = STATUS_META[data.status];
  const isStart = data.nodeType === 'core.start';
  const isEnd = data.nodeType === 'core.end';

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
      {!isStart && (
        <Handle
          type="target"
          position={Position.Left}
          className="execution-flow-node__handle"
        />
      )}
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
      {!isEnd && (
        <Handle
          type="source"
          position={Position.Right}
          className="execution-flow-node__handle"
        />
      )}
    </div>
  );
});

ExecutionNode.displayName = 'ExecutionNode';

const nodeTypes = {
  executionNode: ExecutionNode,
};

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
  defaultViewport,
  fitView = false,
  children,
}) {
  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={readonly ? undefined : onNodesChange}
      onEdgesChange={readonly ? undefined : onEdgesChange}
      onConnect={readonly ? undefined : onConnect}
      onNodeClick={onNodeClick}
      onEdgeClick={onEdgeClick}
      onPaneClick={onPaneClick}
      onDrop={readonly ? undefined : onDrop}
      onDragOver={readonly ? undefined : onDragOver}
      onInit={onInit}
      defaultViewport={defaultViewport}
      nodesDraggable={!readonly}
      nodesConnectable={!readonly}
      elementsSelectable
      deleteKeyCode={null}
      fitView={fitView}
      minZoom={0.25}
      maxZoom={1.8}
      proOptions={{ hideAttribution: true }}
      className={readonly ? 'execution-flow is-readonly' : 'execution-flow'}
    >
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
    </ReactFlow>
  );
}
