import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Card,
  Descriptions,
  Space,
  Tag,
} from 'antd';
import { getExecutionRunExternalOperations } from '../../api/execution';

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

// 左侧“本次执行信息”中的旧系统上传卡片：
// - 预检通过后由服务端直接交付，不依赖页面挂载或倒计时；
// - approved/in_progress 仅显示处理状态；
// - reconciliation_required 指引到右侧标签页做人工对账。
export default function ExecutionAutoApprovalCard({
  runId,
  refreshKey,
  onPresenceChange,
}) {
  const [operations, setOperations] = useState([]);

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
  const resultContract = summary.result_contract || {};
  const judgementContract = summary.judgement_contract || {};
  const files = Array.isArray(summary.files) ? summary.files : [];
  const executionAvailable = summary?.safety?.execution_available !== false;

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
    const activeStatus = active.status === 'in_progress' ? '连接器正在写入旧系统' : '服务端已交付，等待连接器领取';
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
          description="无需停留在当前页面，完成后流程会自动推进；异常会进入人工对账。"
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
      extra={(
        <Tag color={executionAvailable ? 'processing' : 'default'}>
          {executionAvailable ? '自动交付中' : '仅预检'}
        </Tag>
      )}
    >
      <Alert
        style={{ marginBottom: 12 }}
        showIcon
        type={executionAvailable ? 'info' : 'warning'}
        message={`${operationTitle(prepared)}：预检信息`}
        description={executionAvailable
          ? '预检通过后由服务端直接交付 Windows Bridge；离开本页面不会中断处理。'
          : '当前部署未开放对应写入能力，本操作只保留预检信息。'}
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
          ...(business.sample_identity ? [{
            key: 'sample-identity',
            label: '样品识别',
            children: business.sample_identity,
          }] : []),
          ...(summary.inspector ? [{
            key: 'inspector',
            label: '检验员',
            children: summary.inspector,
          }] : []),
          ...(resultContract.value ? [{
            key: 'paper-result',
            label: `${resultContract.worksheet || 'Sheet1'}!${resultContract.cell || 'W32'}`,
            children: `${resultContract.value}${resultContract.unit || ''}`,
          }] : []),
          ...(judgementContract.required === true ? [{
            key: 'judge-basis',
            label: '判定依据',
            children: judgementContract.judge_basis || '—',
          }, {
            key: 'judgement',
            label: '判定结果',
            children: judgementContract.judgement || '—',
          }, {
            key: 'standard-value',
            label: '标准值与允差（人工确认）',
            children: judgementContract.standard_value || '—',
          }] : []),
        ]}
      />
      {!executionAvailable && (
        <Tag color="blue" style={{ marginTop: 8 }}>写入能力未开放</Tag>
      )}
    </Card>
  );
}
