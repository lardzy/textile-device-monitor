import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Empty,
  List,
  Modal,
  Space,
  Spin,
  Steps,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  CopyOutlined,
  EditOutlined,
  FileProtectOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import {
  copyExecutionMutation,
  getExecutionPublishReceipt,
  getExecutionRunMutations,
  preflightExecutionMutation,
  publishExecutionMutation,
  verifyExecutionMutation,
  writeExecutionMutation,
} from '../../api/execution';
import { useExecutionAuth } from './ExecutionAuthContext';

const { Paragraph, Text } = Typography;

const STATUS_META = {
  planned: { label: '预检完成', color: 'blue', step: 0 },
  prepared: { label: '工作副本已生成', color: 'cyan', step: 1 },
  written: { label: '字段已写入', color: 'processing', step: 2 },
  verified: { label: '重读核对通过', color: 'gold', step: 3 },
  published: { label: '已发布', color: 'success', step: 4 },
  failed: { label: '处理失败', color: 'error', step: 0 },
};

const STAGES = [
  { title: '预检' },
  { title: '副本' },
  { title: '写入' },
  { title: '核对' },
  { title: '发布' },
];

const STAGE_NODE_TYPES = {
  copy: 'workbook.copy',
  write: 'workbook.write_cells',
  verify: 'workbook.verify',
  publish: 'artifact.publish',
};

const mutationConflictMessage = (error) => {
  const messages = {
    mutation_node_not_reached: '当前写入节点尚未进入实际执行路径，已刷新运行状态',
    mutation_node_input_mismatch: '流程节点输入已经变化，已刷新服务器保存的输入',
    mutation_node_binding_conflict: '此变更已绑定其他节点，已刷新绑定信息',
    mutation_node_binding_missing: '服务器尚未建立此阶段的节点绑定，已刷新运行状态',
    mutation_node_definition_missing: '运行快照缺少对应节点定义，已刷新运行状态',
    mutation_node_missing: '当前运行缺少对应写入节点，已刷新运行状态',
    mutation_node_required: '当前阶段需要明确指定流程节点，已刷新运行状态',
    mutation_capability_not_declared: '当前运行没有声明该写入阶段，已刷新运行状态',
    publish_confirmation_required: '发布确认尚未完成或已经失效，已刷新人工任务',
    mutation_publish_in_progress: '另一请求正在发布此制品，已刷新发布状态',
  };
  return messages[error?.code] || '文件状态或处理版本已变化，正在刷新最新状态';
};

const nodeRunsOf = run => (
  Array.isArray(run?.nodes)
    ? run.nodes
    : Array.isArray(run?.node_runs)
      ? run.node_runs
      : []
);

const resolveStageNode = (run, mutation, stage) => {
  const nodeType = STAGE_NODE_TYPES[stage];
  const candidates = nodeRunsOf(run).filter(node => node.node_type === nodeType);
  const bindingId = mutation?.change_plan?.node_bindings?.[stage]
    || (stage === 'copy' ? mutation?.node_run_id : null);
  if (bindingId) {
    const bound = candidates.find(node => String(node.id) === String(bindingId));
    return {
      node: bound || null,
      ready: Boolean(bound && ['ready', 'running'].includes(bound.status)),
      reason: bound ? null : '服务器记录的节点绑定与当前运行快照不一致',
    };
  }

  const reached = candidates.filter(node => ['ready', 'running'].includes(node.status));
  if (reached.length === 1) {
    return { node: reached[0], ready: true, reason: null };
  }
  if (reached.length > 1) {
    return {
      node: null,
      ready: false,
      reason: '实际执行路径中存在多个同类型节点，暂不能自动选择',
    };
  }
  if (candidates.length === 1) {
    return {
      node: candidates[0],
      ready: false,
      reason: `节点尚未到达（当前状态：${candidates[0].status || 'unknown'}）`,
    };
  }
  return {
    node: null,
    ready: false,
    reason: candidates.length
      ? '流程包含多个同类型节点，但尚未确定实际执行路径'
      : '当前运行快照中没有对应的写入节点',
  };
};

const artifactRef = (value) => {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const rootId = value.root_id || value.rootId;
  const relativePath = value.relative_path || value.relativePath;
  if (!rootId || !relativePath) {
    return null;
  }
  return {
    root_id: String(rootId),
    relative_path: String(relativePath),
  };
};

const formatArtifact = value => (
  value
    ? `${value.root_id || value.rootId || '—'} / ${value.relative_path || value.relativePath || '—'}`
    : '—'
);

const formatRequestError = (error) => (
  error?.requestId
    ? `${error.message || '操作失败'}（请求编号：${error.requestId}）`
    : error?.message || '操作失败'
);

const JsonPreview = ({ value, empty = '暂无数据' }) => {
  if (!value || (typeof value === 'object' && Object.keys(value).length === 0)) {
    return <Text type="secondary">{empty}</Text>;
  }
  return (
    <pre className="execution-mutation-json">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
};

function ApprovalSummary({ context }) {
  if (!context) {
    return <Text type="secondary">尚未生成发布确认上下文</Text>;
  }
  return (
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label="变更编号">{context.mutation_id || '—'}</Descriptions.Item>
      <Descriptions.Item label="工作副本">{formatArtifact(context.working_copy)}</Descriptions.Item>
      <Descriptions.Item label="工作副本校验和">
        <Text code copyable>{context.working_copy?.content_sha256 || '—'}</Text>
      </Descriptions.Item>
      <Descriptions.Item label="发布目标">{formatArtifact(context.target)}</Descriptions.Item>
      <Descriptions.Item label="变更计划校验和">
        <Text code copyable>{context.change_plan_checksum || '—'}</Text>
      </Descriptions.Item>
    </Descriptions>
  );
}

export default function ExecutionMutationPanel({
  runId,
  run,
  definition,
  artifacts = [],
  refreshKey,
  onRunRefresh,
}) {
  const { hasPermission } = useExecutionAuth();
  const [mutations, setMutations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [actionKey, setActionKey] = useState(null);
  const [receiptDetail, setReceiptDetail] = useState(null);
  const [receiptLoading, setReceiptLoading] = useState(false);

  const nodeTypes = useMemo(
    () => new Set((definition?.nodes || []).filter(node => !node.disabled).map(
      node => node.data?.nodeType || node.type,
    )),
    [definition?.nodes],
  );
  const supportsMutation = nodeTypes.has('workbook.copy')
    || nodeTypes.has('workbook.write_cells')
    || nodeTypes.has('workbook.verify')
    || nodeTypes.has('artifact.publish');

  const load = useCallback(async ({ quiet = false } = {}) => {
    if (!supportsMutation || !hasPermission('file.read')) {
      setMutations([]);
      setLoading(false);
      return;
    }
    if (!quiet) {
      setLoading(true);
    }
    try {
      setMutations(await getExecutionRunMutations(runId));
      setLoadError(null);
    } catch (error) {
      setLoadError(error);
    } finally {
      setLoading(false);
    }
  }, [hasPermission, runId, supportsMutation]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const input = run?.input_data || {};
  const preflightNode = resolveStageNode(run, null, 'copy');
  const copyNodeInput = preflightNode.node?.input_data || {};
  const source = artifactRef(copyNodeInput.source)
    || artifactRef(copyNodeInput.selected_file)
    || artifactRef(copyNodeInput)
    || artifactRef(input.source)
    || artifactRef(artifacts.find(item => item.role === 'source' || item.role === 'input'));
  const targetFromInput = artifactRef(input.target);
  const mutationIdFromInput = String(
    copyNodeInput.mutation_id
    || input.mutation_id
    || `ui-${runId}`,
  );
  const writesFromInput = Array.isArray(input.writes) ? input.writes : null;

  const refreshAfterAction = async () => {
    await Promise.all([
      load({ quiet: true }),
      onRunRefresh?.(),
    ]);
  };

  const runAction = async (key, action) => {
    setActionKey(key);
    setActionError(null);
    try {
      await action();
      message.success('文件变更阶段已完成');
      await refreshAfterAction();
    } catch (error) {
      setActionError(error);
      if (error.status === 409) {
        message.warning(mutationConflictMessage(error));
        await refreshAfterAction();
      } else {
        message.error(formatRequestError(error));
      }
    } finally {
      setActionKey(null);
    }
  };

  const handlePreflight = () => runAction('preflight', () =>
    preflightExecutionMutation(runId, {
      mutation_id: mutationIdFromInput,
      source,
      node_id: preflightNode.node?.node_id,
    }));

  const handleCopy = (mutation, gate) => runAction(`${mutation.mutation_id}:copy`, () =>
    copyExecutionMutation(runId, mutation.mutation_id, {
      node_id: gate.node?.node_id,
    }));

  const writesFor = (mutation, gate) => (
    Array.isArray(gate?.node?.input_data?.writes) && gate.node.input_data.writes.length
      ? gate.node.input_data.writes
      : Array.isArray(mutation.change_plan?.writes) && mutation.change_plan.writes.length
      ? mutation.change_plan.writes
      : writesFromInput
  );

  const targetFor = (mutation, gate) => (
    artifactRef(gate?.node?.input_data?.target)
    || artifactRef(mutation.verification_result?.approval_context?.target)
    || artifactRef(mutation.change_plan?.target)
    || targetFromInput
  );

  const handleWrite = (mutation, gate) => runAction(`${mutation.mutation_id}:write`, () =>
    writeExecutionMutation(runId, mutation.mutation_id, {
      writes: writesFor(mutation, gate),
      node_id: gate.node?.node_id,
    }));

  const handleVerify = (mutation, gate) => runAction(`${mutation.mutation_id}:verify`, () =>
    verifyExecutionMutation(runId, mutation.mutation_id, {
      target: targetFor(mutation, gate),
      writes: writesFor(mutation, gate),
      node_id: gate.node?.node_id,
    }));

  const handlePublish = (mutation, gate) => {
    const approvalContext = mutation.verification_result?.approval_context;
    Modal.confirm({
      title: '确认发布已核对的工作副本？',
      icon: <SafetyCertificateOutlined />,
      width: 620,
      content: (
        <div className="execution-mutation-confirm">
          <Alert
            showIcon
            type="warning"
            message="发布后不会覆盖同名文件；若目标已存在，服务器会拒绝操作。"
          />
          <ApprovalSummary context={approvalContext} />
        </div>
      ),
      okText: '我已核对，确认发布',
      cancelText: '暂不发布',
      onOk: () => runAction(`${mutation.mutation_id}:publish`, () =>
        publishExecutionMutation(runId, mutation.mutation_id, {
          target: targetFor(mutation, gate),
          approval_context: approvalContext,
          node_id: gate.node?.node_id,
        })),
    });
  };

  const openReceipt = async (receipt) => {
    setReceiptLoading(true);
    try {
      setReceiptDetail(await getExecutionPublishReceipt(receipt.id));
    } catch (error) {
      message.error(formatRequestError(error));
    } finally {
      setReceiptLoading(false);
    }
  };

  if (!supportsMutation) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="此流程不包含受控写入步骤"
      />
    );
  }

  if (!hasPermission('file.read')) {
    return (
      <Alert
        showIcon
        type="warning"
        message="当前账号不能查看文件变更"
      />
    );
  }

  if (loading && mutations.length === 0) {
    return (
      <div className="execution-mutation-loading">
        <Spin size="small" />
        <span>正在读取文件变更状态…</span>
      </div>
    );
  }

  const canWrite = hasPermission('file.write');
  const canPublish = hasPermission('file.publish');
  // 与后端文件变更门禁保持一致；暂停中的运行不能绕过执行器推进写入阶段。
  const runActive = [
    'queued',
    'running',
    'waiting_human',
    'waiting_external',
  ].includes(run?.status);

  return (
    <div className="execution-mutation-panel">
      <div className="execution-mutation-panel__header">
        <div>
          <Text strong>受控写入</Text>
          <Paragraph type="secondary">
            原始文件保持不变；所有修改均经过预检、工作副本、重读核对和发布回执。
          </Paragraph>
        </div>
        <Button
          type="text"
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() => load()}
        >
          刷新
        </Button>
      </div>

      {loadError && (
        <Alert
          showIcon
          type="error"
          message="文件变更状态加载失败"
          description={formatRequestError(loadError)}
          action={<Button size="small" onClick={() => load()}>重试</Button>}
        />
      )}
      {actionError && (
        <Alert
          showIcon
          closable
          type={actionError.status === 409 ? 'warning' : 'error'}
          message={actionError.status === 409 ? '状态已变化，已刷新最新数据' : '文件变更操作失败'}
          description={formatRequestError(actionError)}
          onClose={() => setActionError(null)}
        />
      )}

      {mutations.length === 0 ? (
        <div className="execution-mutation-empty">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="尚未创建文件变更计划"
          />
          {!source && (
            <Alert
              showIcon
              type="info"
              message="等待流程生成源文件"
              description="运行输入或源制品中尚无 root_id + relative_path，暂时不能执行预检。"
            />
          )}
          {source && (
            <>
              {runActive && !preflightNode.ready && (
                <Alert
                  showIcon
                  type="info"
                  message="等待工作副本节点进入实际执行路径"
                  description={preflightNode.reason}
                />
              )}
              <Button
                type="primary"
                icon={<FileProtectOutlined />}
                disabled={!canWrite || !runActive || !preflightNode.ready}
                loading={actionKey === 'preflight'}
                onClick={handlePreflight}
              >
                创建写入预检
              </Button>
            </>
          )}
        </div>
      ) : (
        <Collapse
          className="execution-mutation-list"
          defaultActiveKey={[mutations[mutations.length - 1]?.mutation_id]}
          items={mutations.map((mutation) => {
            const status = STATUS_META[mutation.status] || {
              label: mutation.status || '未知状态',
              color: 'default',
              step: 0,
            };
            const copyGate = resolveStageNode(run, mutation, 'copy');
            const writeGate = resolveStageNode(run, mutation, 'write');
            const verifyGate = resolveStageNode(run, mutation, 'verify');
            const publishGate = resolveStageNode(run, mutation, 'publish');
            const writes = writesFor(
              mutation,
              mutation.status === 'prepared' ? writeGate : verifyGate,
            );
            const target = targetFor(
              mutation,
              mutation.status === 'verified' ? publishGate : verifyGate,
            );
            const approvalContext = mutation.verification_result?.approval_context;
            const livePublishWaitsForHuman = run?.mode !== 'test'
              && mutation.status === 'verified';
            return {
              key: mutation.mutation_id,
              label: (
                <Space wrap>
                  <Text strong>{mutation.mutation_id}</Text>
                  <Tag color={status.color}>{status.label}</Tag>
                </Space>
              ),
              children: (
                <div className="execution-mutation">
                  <Steps
                    size="small"
                    current={status.step}
                    status={mutation.status === 'failed' ? 'error' : 'process'}
                    items={STAGES}
                    responsive={false}
                  />
                  <Descriptions size="small" column={1} bordered>
                    <Descriptions.Item label="源文件">
                      {formatArtifact(mutation.source)}
                    </Descriptions.Item>
                    <Descriptions.Item label="工作副本">
                      {formatArtifact(mutation.working_copy)}
                    </Descriptions.Item>
                    <Descriptions.Item label="更新时间">
                      {mutation.updated_at
                        ? new Date(mutation.updated_at).toLocaleString()
                        : '—'}
                    </Descriptions.Item>
                  </Descriptions>

                  {mutation.error && (
                    <Alert
                      showIcon
                      type="error"
                      message={mutation.error.message || '文件变更失败'}
                      description={mutation.error.code}
                    />
                  )}

                  <Collapse
                    ghost
                    size="small"
                    items={[
                      {
                        key: 'plan',
                        label: '变更预检清单',
                        children: <JsonPreview value={mutation.change_plan} empty="暂无变更计划" />,
                      },
                      {
                        key: 'verification',
                        label: '重读核对结果',
                        children: (
                          <>
                            <JsonPreview
                              value={mutation.verification_result}
                              empty="尚未进行重读核对"
                            />
                            {approvalContext && (
                              <>
                                <Text strong>发布确认上下文</Text>
                                <ApprovalSummary context={approvalContext} />
                              </>
                            )}
                          </>
                        ),
                      },
                    ]}
                  />

                  {mutation.publish_receipts?.length > 0 && (
                    <List
                      size="small"
                      header={<Text strong>发布回执</Text>}
                      dataSource={mutation.publish_receipts}
                      renderItem={receipt => (
                        <List.Item
                          actions={[
                            <Button
                              key="detail"
                              type="link"
                              size="small"
                              loading={receiptLoading}
                              onClick={() => openReceipt(receipt)}
                            >
                              查看回执
                            </Button>,
                          ]}
                        >
                          <List.Item.Meta
                            avatar={<CheckCircleOutlined className="is-completed" />}
                            title={formatArtifact({
                              root_id: receipt.target_root_id,
                              relative_path: receipt.target_relative_path,
                            })}
                            description={receipt.content_sha256}
                          />
                        </List.Item>
                      )}
                    />
                  )}

                  {runActive && canWrite && mutation.status === 'planned' && (
                    <>
                      {!copyGate.ready && (
                        <Alert showIcon type="info" message="等待复制节点" description={copyGate.reason} />
                      )}
                      <Button
                        icon={<CopyOutlined />}
                        disabled={!copyGate.ready}
                        loading={actionKey === `${mutation.mutation_id}:copy`}
                        onClick={() => handleCopy(mutation, copyGate)}
                      >
                        生成工作副本
                      </Button>
                    </>
                  )}
                  {runActive && canWrite && mutation.status === 'prepared' && (
                    <>
                      {!writeGate.ready && (
                        <Alert showIcon type="info" message="等待写入节点" description={writeGate.reason} />
                      )}
                      <Button
                        icon={<EditOutlined />}
                        disabled={!writeGate.ready || !Array.isArray(writes) || writes.length === 0}
                        loading={actionKey === `${mutation.mutation_id}:write`}
                        onClick={() => handleWrite(mutation, writeGate)}
                      >
                        写入映射字段
                      </Button>
                    </>
                  )}
                  {runActive && canWrite && mutation.status === 'written' && (
                    <>
                      {!verifyGate.ready && (
                        <Alert showIcon type="info" message="等待核对节点" description={verifyGate.reason} />
                      )}
                      <Button
                        icon={<SafetyCertificateOutlined />}
                        disabled={!verifyGate.ready || !target}
                        loading={actionKey === `${mutation.mutation_id}:verify`}
                        onClick={() => handleVerify(mutation, verifyGate)}
                      >
                        保存后重读核对
                      </Button>
                    </>
                  )}
                  {livePublishWaitsForHuman && (
                    <Alert
                      showIcon
                      type="warning"
                      message="等待人工确认"
                      description="请在左侧人工任务中核对变更计划；正式运行将由流程发布节点继续执行。"
                    />
                  )}
                  {runActive
                    && run?.mode === 'test'
                    && canPublish
                    && mutation.status === 'verified' && (
                    <Button
                      type="primary"
                      icon={<SafetyCertificateOutlined />}
                      disabled={!publishGate.ready || !target || !approvalContext}
                      loading={actionKey === `${mutation.mutation_id}:publish`}
                      onClick={() => handlePublish(mutation, publishGate)}
                    >
                      核对并发布测试制品
                    </Button>
                  )}
                </div>
              ),
            };
          })}
        />
      )}

      <Modal
        title="发布回执详情"
        open={Boolean(receiptDetail)}
        footer={null}
        width={680}
        onCancel={() => setReceiptDetail(null)}
        destroyOnHidden
      >
        <JsonPreview value={receiptDetail} />
      </Modal>
    </div>
  );
}
