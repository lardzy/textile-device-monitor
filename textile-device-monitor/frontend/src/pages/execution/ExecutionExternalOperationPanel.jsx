import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import dayjs from 'dayjs';
import {
  approveExecutionExternalOperation,
  getExecutionRunExternalOperations,
} from '../../api/execution';

const { Paragraph, Text } = Typography;

const STATUS = {
  prepared: { label: '待最终确认', color: 'warning' },
  approved: { label: '已批准，等待连接器', color: 'processing' },
  in_progress: { label: '旧系统处理中', color: 'processing' },
  completed: { label: '已完成并核对', color: 'success' },
  reused: { label: '已复用既有回执', color: 'success' },
  failed: { label: '处理失败', color: 'error' },
  reconciliation_required: { label: '需要人工对账', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
  expired: { label: '已过期，请重新运行', color: 'default' },
};

const requestErrorMessage = error => (
  error?.requestId
    ? `${error.message || '请求失败'}（请求编号：${error.requestId}）`
    : error?.message || '请求失败'
);

const formatBytes = (value) => {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '—';
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KiB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
};

export default function ExecutionExternalOperationPanel({
  runId,
  refreshKey,
  canApprove = false,
  onChanged,
}) {
  const [operations, setOperations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [confirming, setConfirming] = useState(null);
  const [confirmText, setConfirmText] = useState('');
  const [confirmNote, setConfirmNote] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setOperations(await getExecutionRunExternalOperations(runId));
    } catch (error) {
      setLoadError(error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getExecutionRunExternalOperations(runId)
      .then((items) => {
        if (!cancelled) {
          setOperations(items);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setLoadError(error);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, runId]);

  const openConfirmation = (operation) => {
    setConfirming(operation);
    setConfirmText('');
    setConfirmNote('');
  };

  const submitApproval = async () => {
    const sampleNumber = String(
      confirming?.request_summary?.target_sample_number || '',
    );
    if (confirmText.trim() !== sampleNumber) {
      message.warning(`请输入完整样品编号 ${sampleNumber} 以确认`);
      return;
    }
    setSubmitting(true);
    try {
      await approveExecutionExternalOperation(
        confirming.id,
        confirming.payload_checksum,
        confirmText.trim(),
        confirmNote,
      );
      message.success('预检单已批准；当前版本尚未执行旧系统写入');
      setConfirming(null);
      await load();
      await onChanged?.();
    } catch (error) {
      if ([404, 409].includes(error?.status)) {
        message.warning('预检单已变化，正在刷新最新状态');
        setConfirming(null);
        await load();
        await onChanged?.();
      } else {
        message.error(requestErrorMessage(error));
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (loading && operations.length === 0) {
    return <Text type="secondary">正在读取旧系统上传预检单…</Text>;
  }
  if (loadError && operations.length === 0) {
    return (
      <Alert
        showIcon
        type="warning"
        message="旧系统上传预检单读取失败"
        description={requestErrorMessage(loadError)}
        action={<Button size="small" onClick={load}>重试</Button>}
      />
    );
  }
  if (operations.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="流程尚未生成旧系统上传预检单"
      />
    );
  }

  return (
    <>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          showIcon
          type="info"
          message="当前仅生成和批准预检单"
          description="当前版本没有远端领取或提交接口，批准不会连接、复制文件或写入旧检务系统。"
        />
        {operations.map((operation) => {
          const summary = operation.request_summary || {};
          const business = summary.business_fields || {};
          const status = STATUS[operation.status] || {
            label: operation.status || '未知状态',
            color: 'default',
          };
          const files = Array.isArray(summary.files) ? summary.files : [];
          return (
            <Card
              key={operation.id}
              size="small"
              title={(
                <Space>
                  <span>旧系统上传-再生纤-根数法</span>
                  <Tag color={status.color}>{status.label}</Tag>
                </Space>
              )}
              extra={operation.status === 'prepared' && canApprove ? (
                <Button type="primary" onClick={() => openConfirmation(operation)}>
                  核对并批准预检单
                </Button>
              ) : null}
            >
              <Descriptions
                size="small"
                column={1}
                items={[
                  {
                    key: 'sample',
                    label: '样品编号',
                    children: summary.target_sample_number || '—',
                  },
                  ...(summary.source_inspection_number
                    && summary.source_inspection_number !== summary.target_sample_number
                    ? [{
                        key: 'source',
                        label: '源检验编号',
                        children: summary.source_inspection_number,
                      }]
                    : []),
                  {
                    key: 'inspector',
                    label: '检验员',
                    children: summary.inspector || '—',
                  },
                  {
                    key: 'fields',
                    label: '固定业务字段',
                    children: [
                      business.fiber_category,
                      business.inspection_method,
                      business.inspection_item,
                      business.inspection_copies
                        ? `${business.inspection_copies} 份`
                        : null,
                    ].filter(Boolean).join(' · ') || '—',
                  },
                  {
                    key: 'checksum',
                    label: '预检摘要',
                    children: (
                      <Text code title={operation.payload_checksum}>
                        {String(operation.payload_checksum || '').slice(0, 16)}…
                      </Text>
                    ),
                  },
                  {
                    key: 'approved',
                    label: '批准时间',
                    children: operation.approval?.approved_at
                      ? dayjs(operation.approval.approved_at).format('YYYY-MM-DD HH:mm:ss')
                      : '尚未批准',
                  },
                  {
                    key: 'expires',
                    label: operation.approval?.expires_at
                      ? '批准有效至'
                      : '预检有效至',
                    children: (
                      operation.approval?.expires_at
                      || operation.preflight_expires_at
                    )
                      ? dayjs(
                        operation.approval?.expires_at
                        || operation.preflight_expires_at,
                      ).format('YYYY-MM-DD HH:mm:ss')
                      : '—',
                  },
                ]}
              />
              <Table
                size="small"
                pagination={false}
                rowKey="id"
                dataSource={files}
                columns={[
                  {
                    title: '文件',
                    dataIndex: 'filename',
                    ellipsis: true,
                    render: (value, row) => (
                      <Space size={4}>
                        <span title={row.relative_path}>{value || '未命名文件'}</span>
                        {row.is_primary && <Tag color="gold">主单</Tag>}
                      </Space>
                    ),
                  },
                  {
                    title: '大小',
                    dataIndex: 'size_bytes',
                    width: 88,
                    render: formatBytes,
                  },
                  {
                    title: 'SHA-256',
                    dataIndex: 'content_sha256',
                    width: 150,
                    render: value => (
                      <Text code title={value}>
                        {String(value || '').slice(0, 12)}…
                      </Text>
                    ),
                  },
                ]}
              />
              {operation.error && (
                <Alert
                  style={{ marginTop: 12 }}
                  showIcon
                  type="error"
                  message={operation.error.message || operation.error.code}
                />
              )}
            </Card>
          );
        })}
      </Space>

      <Modal
        title="最终核对本次旧系统上传预检单"
        open={Boolean(confirming)}
        okText="批准预检单（当前不上传）"
        cancelText="返回检查"
        confirmLoading={submitting}
        onCancel={() => !submitting && setConfirming(null)}
        onOk={submitApproval}
        destroyOnHidden
      >
        <Alert
          showIcon
          type="warning"
          message="请逐项核对样品编号、检验员、业务字段和文件摘要"
          description="当前版本点击批准只保存本地审核状态，不会触发旧检务系统写入。"
        />
        <Paragraph style={{ marginTop: 16, marginBottom: 6 }}>
          请输入完整样品编号
          <Text strong>
            {' '}
            {confirming?.request_summary?.target_sample_number}
            {' '}
          </Text>
          以确认：
        </Paragraph>
        <Input
          aria-label="确认样品编号"
          autoComplete="off"
          value={confirmText}
          onChange={event => setConfirmText(event.target.value)}
        />
        <Paragraph style={{ marginTop: 12, marginBottom: 6 }}>审核备注（可选）</Paragraph>
        <Input.TextArea
          aria-label="审核备注"
          rows={2}
          maxLength={1000}
          value={confirmNote}
          onChange={event => setConfirmNote(event.target.value)}
        />
      </Modal>
    </>
  );
}
