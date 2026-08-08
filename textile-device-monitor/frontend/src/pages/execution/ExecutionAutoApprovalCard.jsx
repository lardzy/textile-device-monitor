import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  message,
  Progress,
  Space,
  Tag,
} from 'antd';
import {
  approveExecutionExternalOperation,
  getExecutionRunExternalOperations,
} from '../../api/execution';

const AUTO_APPROVE_SECONDS = 5;

const OPERATION_META = {
  legacy_regenerated_fiber_count_upload: '旧系统上传-再生纤-根数法',
  legacy_special_wool_image_upload: '旧系统上传-特种毛-微观形貌',
  legacy_special_wool_review: '旧系统-特纤复核',
  legacy_microscopy_check_record_entry: '旧系统-检验记录登记与校对',
  legacy_special_wool_qualitative_upload: '旧系统上传-纸类定性原始记录',
  legacy_special_wool_qualitative_review: '旧系统-纸类特纤复核',
  legacy_generic_check_record_entry: '旧系统-检验记录登记（只保存）',
};

const operationTitle = operation => (
  OPERATION_META[operation?.request_summary?.operation_type]
  || '旧系统外部操作'
);

const requestErrorMessage = error => (
  error?.requestId
    ? `${error.message || '请求失败'}（请求编号：${error.requestId}）`
    : error?.message || '请求失败'
);

// 左侧“本次执行信息”中的旧系统上传卡片：
// - prepared 状态给出 5 秒倒计时自动批准，期间可终止转为手动；
// - approved/in_progress 仅显示处理状态；
// - reconciliation_required 指引到右侧标签页做人工对账。
export default function ExecutionAutoApprovalCard({
  runId,
  refreshKey,
  canApprove = false,
  onChanged,
  onPresenceChange,
}) {
  const [operations, setOperations] = useState([]);
  const [secondsLeft, setSecondsLeft] = useState(AUTO_APPROVE_SECONDS);
  const [halted, setHalted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const firingRef = useRef(false);

  const load = useCallback(async () => {
    try {
      setOperations(await getExecutionRunExternalOperations(runId));
    } catch {
      // 快照轮询失败时保留现状，下一次刷新再试
    }
  }, [runId]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  // 轻量自轮询兜底：预检单出现/状态推进不总是改变 refreshKey
  useEffect(() => {
    const timer = window.setInterval(load, 10000);
    return () => window.clearInterval(timer);
  }, [load]);

  const prepared = operations.find(operation => operation.status === 'prepared') || null;
  const active = !prepared
    ? operations.find(operation => ['approved', 'in_progress'].includes(operation.status)) || null
    : null;
  const reconciliation = !prepared && !active
    ? operations.find(operation => operation.status === 'reconciliation_required') || null
    : null;

  // 卡片始终挂载在左栏；回报有无内容，驱动左栏加宽与“需要您处理”标题
  useEffect(() => {
    onPresenceChange?.(Boolean(prepared || active || reconciliation));
  }, [onPresenceChange, prepared, active, reconciliation]);

  const summary = prepared?.request_summary || {};
  const business = summary.business_fields || {};
  const files = Array.isArray(summary.files) ? summary.files : [];
  const executionAvailable = summary?.safety?.execution_available !== false;
  const autoApprovable = Boolean(prepared && canApprove && executionAvailable);

  const approve = useCallback(async (note) => {
    if (!prepared || firingRef.current) {
      return;
    }
    firingRef.current = true;
    setSubmitting(true);
    try {
      await approveExecutionExternalOperation(
        prepared.id,
        prepared.payload_checksum,
        String(summary.target_sample_number || ''),
        note,
      );
      message.success('预检单已批准，Windows Bridge 将立即领取');
      await load();
      await onChanged?.();
    } catch (error) {
      if ([404, 409].includes(error?.status)) {
        message.warning('预检单已变化，正在刷新最新状态');
        await load();
        await onChanged?.();
      } else {
        message.error(requestErrorMessage(error));
        setHalted(true);
      }
    } finally {
      firingRef.current = false;
      setSubmitting(false);
    }
  }, [prepared, summary.target_sample_number, load, onChanged]);

  // 换一张预检单时重置倒计时
  useEffect(() => {
    setSecondsLeft(AUTO_APPROVE_SECONDS);
    setHalted(false);
    firingRef.current = false;
  }, [prepared?.id]);

  useEffect(() => {
    if (!autoApprovable || halted || submitting) {
      return undefined;
    }
    if (secondsLeft <= 0) {
      approve('5 秒倒计时自动批准');
      return undefined;
    }
    const timer = window.setTimeout(
      () => setSecondsLeft(value => value - 1),
      1000,
    );
    return () => window.clearTimeout(timer);
  }, [autoApprovable, halted, submitting, secondsLeft, approve]);

  if (!prepared && !active && !reconciliation) {
    return null;
  }

  if (reconciliation) {
    return (
      <Card size="small" className="execution-auto-approval" title="旧系统上传">
        <Alert
          showIcon
          type="error"
          message={`${operationTitle(reconciliation)}：远端副作用未知，需要管理员人工对账`}
          description="本操作不可自动重领或重试，请切换到右侧「旧系统上传」标签页，由管理员核对证据后录入对账结论。"
        />
      </Card>
    );
  }

  if (active) {
    const activeStatus = active.status === 'in_progress' ? '连接器正在写入旧系统' : '已批准，等待连接器领取';
    return (
      <Card size="small" className="execution-auto-approval" title="旧系统上传">
        <Alert
          showIcon
          type="info"
          message={(
            <Space size={8}>
              <span>{operationTitle(active)}</span>
              <Tag color="processing">{activeStatus}</Tag>
            </Space>
          )}
          description="批准后无需停留等待，完成后流程会自动推进；异常会进入人工对账。"
        />
      </Card>
    );
  }

  const targetFilename = summary.target_filename
    || files[0]?.target_filename
    || files[0]?.filename
    || '—';

  return (
    <Card
      size="small"
      className="execution-auto-approval"
      title="旧系统上传"
      extra={<Tag color="warning">待批准</Tag>}
    >
      <Alert
        style={{ marginBottom: 12 }}
        showIcon
        type="warning"
        message={`${operationTitle(prepared)}：请核对以下信息`}
        description="倒计时结束将自动批准并交付 Windows Bridge 写入旧检务系统；需要人工核对或暂停时请点击「终止自动批准」。"
      />
      <Descriptions
        size="small"
        column={1}
        items={[
          {
            key: 'sample',
            label: '样品编号',
            children: summary.target_sample_number || '—',
          },
          {
            key: 'filename',
            label: '目标文件名',
            children: targetFilename,
          },
          ...(business.review_action || business.inspection_item || business.review_item ? [{
            key: 'business',
            label: '业务字段',
            children: [
              business.fiber_category,
              business.inspection_method,
              business.inspection_item || business.review_item,
              business.review_action,
            ].filter(Boolean).join(' / ') || '—',
          }] : []),
          ...(summary.inspector ? [{
            key: 'inspector',
            label: '检验员',
            children: summary.inspector,
          }] : []),
        ]}
      />
      {!executionAvailable && (
        <Tag color="blue" style={{ marginTop: 8 }}>仅预检（能力未开放，不会自动批准）</Tag>
      )}
      {!canApprove && (
        <Alert
          style={{ marginTop: 12 }}
          showIcon
          type="info"
          message="当前账号无批准权限，请等待有权限的账号处理"
        />
      )}
      {autoApprovable && !halted && (
        <div style={{ marginTop: 12 }}>
          <Progress
            percent={((AUTO_APPROVE_SECONDS - secondsLeft) / AUTO_APPROVE_SECONDS) * 100}
            showInfo={false}
            status="active"
            size="small"
          />
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <span>{secondsLeft} 秒后自动批准</span>
            <Space>
              <Button size="small" onClick={() => setHalted(true)}>
                终止自动批准
              </Button>
              <Button
                size="small"
                type="primary"
                loading={submitting}
                onClick={() => approve('人工立即批准')}
              >
                立即批准
              </Button>
            </Space>
          </Space>
        </div>
      )}
      {autoApprovable && halted && (
        <Space style={{ marginTop: 12 }}>
          <Tag color="default">自动批准已终止</Tag>
          <Button
            size="small"
            type="primary"
            loading={submitting}
            onClick={() => approve('终止后人工批准')}
          >
            核对无误，批准此预检单
          </Button>
        </Space>
      )}
    </Card>
  );
}
