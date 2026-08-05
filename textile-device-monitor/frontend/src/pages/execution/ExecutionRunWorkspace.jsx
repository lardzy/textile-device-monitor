import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  List,
  Modal,
  Result,
  Space,
  Spin,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  FileOutlined,
  LoadingOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SyncOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  cancelExecutionRun,
  getExecutionRun,
  getExecutionRunEventHistory,
  pauseExecutionRun,
  resumeExecutionRun,
  retryExecutionNode,
} from '../../api/execution';
import {
  mergeNodeRunStatuses,
  normalizeWorkflowDefinition,
  resolveExecutionFocusNodeIds,
} from '../../utils/executionWorkflow';
import { useExecutionAuth } from './ExecutionAuthContext';
import ExecutionChrome from './ExecutionChrome';
import ExecutionExternalOperationPanel from './ExecutionExternalOperationPanel';
import ExecutionMutationPanel from './ExecutionMutationPanel';
import ExecutionResultFiles, {
  extractExecutionResultFiles,
  extractPrimaryFileId,
} from './ExecutionResultFiles';
import HumanTaskCard from './HumanTaskCard';
import SchemaFields from './SchemaFields';
import WorkflowCanvas from './WorkflowCanvas';
import useExecutionEvents from './useExecutionEvents';
import './execution.css';

const { Text, Title } = Typography;

const RUN_STATUS = {
  created: { label: '已创建', color: 'default' },
  pending: { label: '等待执行', color: 'default' },
  queued: { label: '已排队', color: 'processing' },
  running: { label: '执行中', color: 'processing' },
  waiting_human: { label: '等待人工处理', color: 'warning' },
  waiting_external: { label: '等待旧系统处理', color: 'warning' },
  paused: { label: '已暂停', color: 'warning' },
  cancel_pending: { label: '发布收尾后取消', color: 'warning' },
  failure_pending: { label: '发布核对后失败', color: 'error' },
  completed: { label: '已完成', color: 'success' },
  succeeded: { label: '已完成', color: 'success' },
  failed: { label: '执行失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
};

const terminalStatuses = new Set(['completed', 'succeeded', 'failed', 'cancelled']);

const requestErrorDescription = error => (
  error?.requestId
    ? `${error.message || '请求失败'}（请求编号：${error.requestId}）`
    : error?.message || '请求失败'
);

const eventSequence = (event) => {
  const sequence = Number(event?.sequence);
  return Number.isFinite(sequence) ? sequence : null;
};

const mergeExecutionEvents = (...groups) => {
  const merged = new Map();
  groups.flat().forEach((event) => {
    if (!event) {
      return;
    }
    const sequence = eventSequence(event);
    const key = sequence !== null
      ? `sequence:${sequence}`
      : event.event_id
        ? `event:${event.event_id}`
        : `${event.type || 'event'}:${event.occurred_at || ''}`;
    merged.set(key, event);
  });
  return [...merged.values()].sort((left, right) => {
    const leftSequence = eventSequence(left);
    const rightSequence = eventSequence(right);
    if (leftSequence !== null && rightSequence !== null) {
      return leftSequence - rightSequence;
    }
    if (leftSequence !== null) {
      return -1;
    }
    if (rightSequence !== null) {
      return 1;
    }
    return String(left.occurred_at || '').localeCompare(String(right.occurred_at || ''));
  });
};

const unwrapSnapshot = (payload) => {
  const run = payload?.run || payload;
  return {
    ...payload,
    run,
    definition: normalizeWorkflowDefinition(
      payload?.definition
      || payload?.workflow_version?.definition
      || run?.workflow_definition
      || run?.definition,
      { name: run?.workflow_name },
    ),
    nodeRuns: payload?.node_runs || run?.node_runs || run?.nodes || [],
    humanTasks: payload?.human_tasks || run?.human_tasks || [],
    artifacts: payload?.artifacts || run?.artifacts || [],
    events: payload?.events || run?.events || [],
    outputs: payload?.outputs || run?.outputs || run?.output_data || {},
  };
};

export default function ExecutionRunWorkspace() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const {
    canHandleHumanTasks,
    canRunWorkflow,
    canReconcileExternalOperations,
  } = useExecutionAuth();
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [actionLoading, setActionLoading] = useState(null);
  const [connection, setConnection] = useState('connecting');
  const [detailOpen, setDetailOpen] = useState(false);
  const [eventHistory, setEventHistory] = useState({
    runId: null,
    initialized: false,
    hasMore: false,
    loading: false,
    error: null,
  });
  const requestSeqRef = useRef(0);
  const refreshTimerRef = useRef(null);

  const loadSnapshot = useCallback(async ({ quiet = false } = {}) => {
    const seq = ++requestSeqRef.current;
    if (!quiet) {
      setLoading(true);
    }
    try {
      const payload = await getExecutionRun(runId);
      if (seq === requestSeqRef.current) {
        const nextSnapshot = unwrapSnapshot(payload);
        setSnapshot(current => (
          current?.run?.id === nextSnapshot.run?.id
            ? {
              ...nextSnapshot,
              events: mergeExecutionEvents(
                current.events || [],
                nextSnapshot.events || [],
              ),
            }
            : nextSnapshot
        ));
        setEventHistory(current => (
          current.runId !== nextSnapshot.run?.id || !current.initialized
            ? {
              runId: nextSnapshot.run?.id || runId,
              initialized: true,
              hasMore: Boolean(
                nextSnapshot.event_history?.has_more
                ?? ((nextSnapshot.events || []).length >= 100)
              ),
              loading: false,
              error: null,
            }
            : current
        ));
        setError(null);
      }
    } catch (requestError) {
      if (seq === requestSeqRef.current) {
        setError(requestError);
      }
    } finally {
      if (!quiet && seq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, [runId]);

  useEffect(() => {
    setEventHistory({
      runId,
      initialized: false,
      hasMore: false,
      loading: false,
      error: null,
    });
    loadSnapshot();
    return () => {
      requestSeqRef.current += 1;
      if (refreshTimerRef.current) {
        window.clearTimeout(refreshTimerRef.current);
      }
    };
  }, [loadSnapshot]);

  const loadEarlierEvents = useCallback(async () => {
    const sequences = (snapshot?.events || [])
      .map(eventSequence)
      .filter(sequence => sequence !== null);
    const beforeId = sequences.length ? Math.min(...sequences) : null;
    if (beforeId === null || eventHistory.loading) {
      return;
    }

    const requestedRunId = runId;
    setEventHistory(current => (
      current.runId === requestedRunId
        ? { ...current, loading: true, error: null }
        : current
    ));
    try {
      const page = await getExecutionRunEventHistory(requestedRunId, {
        before_id: beforeId,
        limit: 100,
      });
      setSnapshot(current => (
        current?.run?.id === requestedRunId
          ? {
            ...current,
            events: mergeExecutionEvents(
              page?.items || [],
              current.events || [],
            ),
          }
          : current
      ));
      setEventHistory(current => (
        current.runId === requestedRunId
          ? {
            ...current,
            initialized: true,
            hasMore: Boolean(page?.has_more),
            loading: false,
            error: null,
          }
          : current
      ));
    } catch (requestError) {
      setEventHistory(current => (
        current.runId === requestedRunId
          ? {
            ...current,
            loading: false,
            error: requestError.message || '更早动态加载失败',
          }
          : current
      ));
    }
  }, [eventHistory.loading, runId, snapshot?.events]);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimerRef.current) {
      return;
    }
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = null;
      loadSnapshot({ quiet: true });
    }, 180);
  }, [loadSnapshot]);

  useExecutionEvents(runId, {
    enabled: Boolean(snapshot && !terminalStatuses.has(snapshot.run?.status)),
    onEvent: scheduleRefresh,
    onReconnect: () => loadSnapshot({ quiet: true }),
    onConnectionChange: setConnection,
  });

  const performRunAction = async (action) => {
    setActionLoading(action);
    try {
      if (action === 'pause') {
        await pauseExecutionRun(runId);
      } else if (action === 'resume') {
        await resumeExecutionRun(runId);
      } else if (action === 'cancel') {
        const confirmed = await new Promise((resolve) => {
          Modal.confirm({
            title: '取消本次执行？',
            content: '已完成的节点会保留，但未执行节点将不会继续。',
            okText: '确认取消',
            cancelText: '返回',
            okButtonProps: { danger: true },
            // Ant Design 会把带一个参数的回调当成“调用方自行关闭”。
            // 不能在此直接传 Promise.resolve，否则请求会执行但弹窗不会关闭。
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!confirmed) {
          return;
        }
        await cancelExecutionRun(runId);
      }
      await loadSnapshot({ quiet: true });
    } catch (requestError) {
      message.error(requestErrorDescription(requestError));
    } finally {
      setActionLoading(null);
    }
  };

  const retryNode = async (nodeId) => {
    try {
      await retryExecutionNode(runId, nodeId);
      message.success('节点已重新排队');
      loadSnapshot({ quiet: true });
    } catch (requestError) {
      message.error(requestErrorDescription(requestError));
    }
  };

  if (loading && !snapshot) {
    return (
      <div className="execution-route-loading">
        <Spin size="large" />
        <span>正在恢复执行现场…</span>
      </div>
    );
  }

  if (error && !snapshot) {
    return (
      <Result
        status={error.status === 404 ? '404' : 'error'}
        title={error.status === 404 ? '未找到此执行记录' : '执行记录加载失败'}
        subTitle={requestErrorDescription(error)}
        extra={<Button type="primary" onClick={() => navigate('/execution')}>返回流程目录</Button>}
      />
    );
  }

  const run = snapshot.run || {};
  const status = RUN_STATUS[run.status] || { label: run.status || '未知状态', color: 'default' };
  const nodes = mergeNodeRunStatuses(snapshot.definition.nodes, snapshot.nodeRuns);
  const focusNodeIds = resolveExecutionFocusNodeIds(
    snapshot.nodeRuns,
    snapshot.definition.nodes,
    run.status,
  );
  const activeHumanTasks = snapshot.humanTasks.filter(task =>
    ['pending', 'open', 'claimed'].includes(task.status),
  );
  const inputSchema = snapshot.definition.input_schema;
  const globalSchema = snapshot.definition.global_schema;
  const variables = run.input_data || run.variables || run.input_values || {};
  const globalVariables = run.global_data || {};
  const resultFiles = extractExecutionResultFiles(snapshot.outputs);
  const primaryResultFileId = extractPrimaryFileId(snapshot.outputs);
  const hasExternalOperations = (snapshot.definition.nodes || []).some(node => (
    String(node.data?.nodeType || node.node_type || node.type || '')
      .startsWith('external.legacy_')
  ));

  const actions = (
    <Space>
      <Tooltip title={connection === 'connected' ? '实时连接正常' : '实时连接正在恢复'}>
        <Badge
          status={connection === 'connected' ? 'success' : 'processing'}
          text={connection === 'connected' ? '实时同步' : '重新连接'}
        />
      </Tooltip>
      <Button icon={<ReloadOutlined />} onClick={() => loadSnapshot()}>刷新</Button>
      {canRunWorkflow && run.status === 'running' && (
        <Button
          icon={<PauseOutlined />}
          loading={actionLoading === 'pause'}
          onClick={() => performRunAction('pause')}
        >
          暂停
        </Button>
      )}
      {canRunWorkflow && run.status === 'paused' && (
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          loading={actionLoading === 'resume'}
          onClick={() => performRunAction('resume')}
        >
          继续
        </Button>
      )}
      {canRunWorkflow
        && !terminalStatuses.has(run.status)
        && run.status !== 'cancel_pending' && (
        <Button danger loading={actionLoading === 'cancel'} onClick={() => performRunAction('cancel')}>
          取消
        </Button>
      )}
    </Space>
  );

  const eventItems = snapshot.events.map((event) => ({
    color: event.level === 'error' ? 'red' : event.level === 'warning' ? 'orange' : 'blue',
    children: (
      <div className="execution-event">
        <strong>{event.title || event.type || '流程事件'}</strong>
        <span>{event.message || event.description || event.payload?.message || event.payload?.status}</span>
        <time>{event.occurred_at ? dayjs(event.occurred_at).format('MM-DD HH:mm:ss') : ''}</time>
      </div>
    ),
  }));

  const rightTabs = [
    {
      key: 'result',
      label: '执行结果',
      children: (
        <div className="execution-result-panel">
          {resultFiles.length ? (
            <ExecutionResultFiles
              files={resultFiles}
              primaryId={primaryResultFileId}
            />
          ) : Object.keys(snapshot.outputs || {}).length ? (
            <Descriptions column={1} size="small">
              {Object.entries(snapshot.outputs || {}).map(([key, value]) => (
                <Descriptions.Item key={key} label={key}>
                  {typeof value === 'object' ? JSON.stringify(value) : String(value ?? '—')}
                </Descriptions.Item>
              ))}
            </Descriptions>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="节点完成后将在这里显示输出" />
          )}
        </div>
      ),
    },
    {
      key: 'artifacts',
      label: `制品 ${snapshot.artifacts.length || ''}`,
      children: (
        <List
          locale={{ emptyText: '暂无制品' }}
          dataSource={snapshot.artifacts}
          renderItem={artifact => (
            <List.Item>
              <List.Item.Meta
                avatar={<FileOutlined className="execution-artifact-icon" />}
                title={artifact.name || artifact.relative_path || artifact.id}
                description={(
                  <Space direction="vertical" size={0}>
                    <span>{artifact.kind || artifact.media_type || '文件制品'}</span>
                    <span>{artifact.status || '已生成'}</span>
                  </Space>
                )}
              />
            </List.Item>
          )}
        />
      ),
    },
    {
      key: 'mutations',
      label: '文件变更',
      children: (
        <ExecutionMutationPanel
          runId={runId}
          run={run}
          definition={snapshot.definition}
          artifacts={snapshot.artifacts}
          refreshKey={run.updated_at}
          onRunRefresh={() => loadSnapshot({ quiet: true })}
        />
      ),
    },
    ...(hasExternalOperations ? [{
      key: 'external-operations',
      label: '旧系统上传',
      children: (
        <ExecutionExternalOperationPanel
          runId={runId}
          refreshKey={run.updated_at}
          canApprove={canRunWorkflow}
          canReconcile={canReconcileExternalOperations}
          onChanged={() => loadSnapshot({ quiet: true })}
        />
      ),
    }] : []),
    {
      key: 'events',
      label: `时间线 ${snapshot.events.length || ''}`,
      children: (
        <div className="execution-event-history">
          {eventHistory.hasMore && !eventHistory.error && (
            <div className="execution-event-history__more">
              <Button
                type="link"
                size="small"
                loading={eventHistory.loading}
                onClick={loadEarlierEvents}
              >
                加载更早动态
              </Button>
            </div>
          )}
          {eventHistory.error && (
            <Alert
              showIcon
              type="warning"
              message={eventHistory.error}
              action={(
                <Button
                  type="link"
                  size="small"
                  loading={eventHistory.loading}
                  onClick={loadEarlierEvents}
                >
                  重试
                </Button>
              )}
            />
          )}
          {eventItems.length
            ? <Timeline items={eventItems} />
            : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无执行事件" />}
        </div>
      ),
    },
    {
      key: 'errors',
      label: '异常',
      children: (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {run.error && (
            <Alert
              showIcon
              type="error"
              message={run.error.message || '流程运行失败'}
              description={run.error.code ? `错误代码：${run.error.code}` : undefined}
            />
          )}
          <List
            locale={{ emptyText: run.error ? '没有额外的节点异常' : '当前没有异常' }}
            dataSource={snapshot.nodeRuns.filter(node => node.status === 'failed')}
            renderItem={node => (
              <List.Item
                actions={[
                  <Button
                    key="retry"
                    size="small"
                    icon={<SyncOutlined />}
                    disabled={!canRunWorkflow}
                    onClick={() => retryNode(node.node_id || node.nodeId)}
                  >
                    重试
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  avatar={<CloseCircleOutlined style={{ color: '#d73a49' }} />}
                  title={node.node_name || node.name || node.node_id}
                  description={node.error_message || node.error?.message || node.message}
                />
              </List.Item>
            )}
          />
        </Space>
      ),
    },
  ];

  return (
    <div className="execution-workspace">
      <ExecutionChrome
        title={run.workflow_name || snapshot.definition.metadata?.name || '执行工作台'}
        subtitle={`运行编号 ${run.id || runId}`}
        backTo={{ path: '/execution/runs', label: '执行记录' }}
        actions={actions}
      />

      {error && (
        <Alert
          banner
          showIcon
          type="warning"
          message={`最近一次刷新失败：${requestErrorDescription(error)}`}
          action={<Button size="small" onClick={() => loadSnapshot()}>重试</Button>}
        />
      )}
      {run.error && (
        <Alert
          banner
          showIcon
          type="error"
          message={run.error.message || '本次运行失败'}
          description={run.error.code ? `错误代码：${run.error.code}` : undefined}
        />
      )}

      <main className="execution-workspace__grid">
        <aside className="execution-workspace__left">
          <div className="execution-workspace__section-title">
            <div>
              <Text className="execution-eyebrow">RUN CONTEXT</Text>
              <Title level={4}>本次执行信息</Title>
            </div>
            <Tag color={status.color}>{status.label}</Tag>
          </div>
          <Form layout="vertical" initialValues={variables} disabled>
            <SchemaFields schema={inputSchema} disabled />
          </Form>
          {Object.keys(globalSchema?.properties || {}).length > 0 && (
            <>
              <Divider orientation="left">已提交的全局变量</Divider>
              <Form layout="vertical" initialValues={globalVariables} disabled>
                <SchemaFields schema={globalSchema} disabled />
              </Form>
            </>
          )}
          <Divider />
          <Descriptions
            title="运行摘要"
            column={1}
            size="small"
            items={[
              { key: 'number', label: '检验编号', children: run.inspection_number || variables.inspection_number || '—' },
              { key: 'version', label: '流程版本', children: run.workflow_version || run.version || '—' },
              { key: 'started', label: '开始时间', children: run.started_at ? dayjs(run.started_at).format('YYYY-MM-DD HH:mm:ss') : '等待开始' },
              { key: 'mode', label: '运行模式', children: run.mode === 'test' ? '测试运行' : '正式运行' },
            ]}
          />
          {activeHumanTasks.length > 0 && (
            <>
              <Divider orientation="left">需要您处理</Divider>
              {canHandleHumanTasks ? (
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  {activeHumanTasks.map(task => (
                    <HumanTaskCard
                      key={task.id}
                      task={task}
                      nodeRun={snapshot.nodeRuns.find(node => (
                        (node.node_id || node.nodeId) === task.node_id
                      ))}
                      inspectionNumber={run.inspection_number || variables.inspection_number}
                      onChanged={() => loadSnapshot({ quiet: true })}
                    />
                  ))}
                </Space>
              ) : (
                <Alert
                  showIcon
                  type="warning"
                  message="当前账号不能处理人工任务"
                  description="请联系拥有人工任务权限的检验员继续本次执行。"
                />
              )}
            </>
          )}
        </aside>

        <section className="execution-workspace__canvas">
          <div className="execution-canvas-toolbar">
            <div>
              <Text strong>流程进度</Text>
              <Text type="secondary">运行中的流程图为只读状态</Text>
            </div>
            <Button size="small" onClick={() => setDetailOpen(true)}>查看节点明细</Button>
          </div>
          <WorkflowCanvas
            nodes={nodes}
            edges={snapshot.definition.edges}
            readonly
            fitView
            focusNodeIds={focusNodeIds}
          />
        </section>

        <aside className="execution-workspace__right">
          <Tabs items={rightTabs} defaultActiveKey="result" />
        </aside>
      </main>

      <Drawer
        title="节点运行明细"
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={520}
      >
        <List
          dataSource={snapshot.nodeRuns}
          locale={{ emptyText: '节点尚未开始执行' }}
          renderItem={(node) => {
            const nodeStatus = RUN_STATUS[node.status] || { label: node.status, color: 'default' };
            return (
              <List.Item
                actions={node.status === 'failed' && canRunWorkflow
                  ? [<Button key="retry" onClick={() => retryNode(node.node_id)}>重试</Button>]
                  : []}
              >
                <List.Item.Meta
                  avatar={node.status === 'failed'
                    ? <WarningOutlined style={{ color: '#d73a49' }} />
                    : node.status === 'running'
                      ? <LoadingOutlined spin />
                      : <CheckCircleOutlined />}
                  title={node.node_name || node.name || node.node_id}
                  description={node.message || node.error_message || node.error?.message}
                />
                <Tag color={nodeStatus.color}>{nodeStatus.label}</Tag>
              </List.Item>
            );
          }}
        />
      </Drawer>
    </div>
  );
}
