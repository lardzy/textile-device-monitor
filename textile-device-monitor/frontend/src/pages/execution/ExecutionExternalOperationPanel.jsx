import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Empty,
  Input,
  InputNumber,
  Modal,
  Radio,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import dayjs from 'dayjs';
import {
  getExecutionExternalOperationReconciliation,
  getExecutionRunExternalOperations,
  reconcileExecutionExternalOperation,
} from '../../api/execution';

const { Paragraph, Text } = Typography;

const STATUS = {
  prepared: { label: '预检已生成', color: 'processing' },
  approved: { label: '已交付，等待连接器', color: 'processing' },
  in_progress: { label: '旧系统处理中', color: 'processing' },
  completed: { label: '已完成并核对', color: 'success' },
  reused: { label: '已复用既有回执', color: 'success' },
  failed: { label: '处理失败', color: 'error' },
  reconciliation_required: { label: '需要人工对账', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
  expired: { label: '已过期，请重新运行', color: 'default' },
};

const OPERATION_META = {
  legacy_regenerated_fiber_count_upload: {
    title: '旧系统上传-再生纤-根数法',
    subject: '上传',
  },
  legacy_special_wool_image_upload: {
    title: '旧系统上传-特种毛-图片',
    subject: '上传',
  },
  legacy_special_wool_review: {
    title: '旧系统-特纤复核',
    subject: '复核',
  },
  legacy_microscopy_check_record_entry: {
    title: '检验记录登记与校对',
    subject: '检验记录登记与校对',
  },
  legacy_special_wool_qualitative_upload: {
    title: '旧系统上传-纸类定性原始记录',
    subject: '纸类原始记录上传',
  },
  legacy_special_wool_qualitative_review: {
    title: '旧系统-纸类特纤复核',
    subject: '纸类特纤复核',
  },
  legacy_generic_check_record_entry: {
    title: '检验记录登记-纸类定性结果',
    subject: '纸类定性结果登记',
  },
};

const PAPER_OPERATION_TYPES = new Set([
  'legacy_special_wool_qualitative_upload',
  'legacy_special_wool_qualitative_review',
  'legacy_generic_check_record_entry',
]);

const isPaperOperation = operation => PAPER_OPERATION_TYPES.has(
  operation?.request_summary?.operation_type,
);

const paperOperationGuidance = (operation) => {
  const type = operation?.request_summary?.operation_type;
  const status = operation?.status;
  if (!PAPER_OPERATION_TYPES.has(type)) {
    return null;
  }
  if (status === 'prepared') {
    if (type === 'legacy_special_wool_qualitative_upload') {
      return '预检已生成；写入能力开启时由服务端自动交付纸类原始记录上传。';
    }
    if (type === 'legacy_special_wool_qualitative_review') {
      return '预检已生成；写入能力开启时由服务端自动交付纸类特纤复核，本步骤不会重复上传文件。';
    }
    if (operation?.request_summary?.judgement_contract?.required === true) {
      return '样品识别、Sheet1!W32 实测值、判定结果及人工确认的“标准值与允差”已绑定预检；服务端会自动交付登记，本步骤只保存，不执行校对。';
    }
    return '样品识别及 Sheet1!W32 实际值已绑定预检；服务端会自动交付登记，本步骤只保存，不执行校对。';
  }
  if (['approved', 'in_progress'].includes(status)) {
    return '预检单已交付，正在等待或执行 Windows Bridge 旧系统操作。';
  }
  if (['completed', 'reused'].includes(status)) {
    return '旧系统回执已核对，本步骤可以继续推进流程。';
  }
  return null;
};

const operationMeta = operation => (
  OPERATION_META[operation?.request_summary?.operation_type]
  || { title: '旧系统外部操作', subject: '操作' }
);

const recordCountTransition = (summary) => {
  const existing = Number(summary?.expected_existing_register_count);
  const resulting = Number(summary?.resulting_register_count);
  if (!Number.isInteger(existing) || !Number.isInteger(resulting)) {
    return '—';
  }
  return `${existing} 条 → ${resulting} 条`;
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

const initialReconciliationForm = {
  action: null,
  checkedAt: '',
  sampleNumber: '',
  note: '',
  exactRecordCount: null,
  containsRecordCount: null,
  targetFileCount: null,
  remoteRecordId: '',
  remoteFileSha256: '',
  businessFieldsMatch: false,
  inspectorMatch: false,
  actualRegisterCount: null,
  actualFileReferenceCount: null,
  actualKeyResultCount: null,
  actualProofedCount: null,
};

const FINAL_ENTRY_RECONCILIATION_CONTRACT = 'microscopy_final_entry_v1';

const isFinalEntryReconciliationContext = context => (
  context?.evidence_kind === FINAL_ENTRY_RECONCILIATION_CONTRACT
  || context?.expected_evidence?.evidence_contract
    === FINAL_ENTRY_RECONCILIATION_CONTRACT
);

const RECONCILIATION_STATE_CHANGED_CODES = new Set([
  'external_operation_node_not_waiting',
  'external_operation_payload_changed',
  'external_operation_run_changed',
  'external_operation_sample_confirmation_mismatch',
  'external_reconciliation_already_resolved',
  'external_reconciliation_attempt_changed',
  'external_reconciliation_attempt_invalid',
  'external_reconciliation_attempt_missing',
  'external_reconciliation_attempt_not_failed',
  'external_reconciliation_not_required',
  'external_reconciliation_prewrite_attempt',
  'external_reconciliation_run_not_active',
]);

const shouldRefreshReconciliation = error => (
  error?.status === 404
  || (
    error?.status === 409
    && RECONCILIATION_STATE_CHANGED_CODES.has(error?.code)
  )
);

export default function ExecutionExternalOperationPanel({
  runId,
  refreshKey,
  canReconcile = false,
  onChanged,
}) {
  const [operations, setOperations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [reconciliationLoadingId, setReconciliationLoadingId] = useState(null);
  const [reconciling, setReconciling] = useState(null);
  const [reconciliationForm, setReconciliationForm] = useState({
    ...initialReconciliationForm,
  });
  const reconciliationContextRequestRef = useRef(0);

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

  const openReconciliation = async (operation) => {
    const requestId = reconciliationContextRequestRef.current + 1;
    reconciliationContextRequestRef.current = requestId;
    setReconciliationLoadingId(operation.id);
    try {
      const context = await getExecutionExternalOperationReconciliation(
        operation.id,
      );
      if (reconciliationContextRequestRef.current !== requestId) {
        return;
      }
      const currentOperation = context?.operation || operation;
      if (currentOperation.status !== 'reconciliation_required') {
        message.warning('待对账操作已变化，正在刷新最新状态');
        await load();
        await onChanged?.();
        return;
      }
      setReconciliationForm({ ...initialReconciliationForm });
      setReconciling({ operation: currentOperation, context });
    } catch (error) {
      if (reconciliationContextRequestRef.current !== requestId) {
        return;
      }
      if (shouldRefreshReconciliation(error)) {
        message.warning('待对账操作已变化，正在刷新最新状态');
        await load();
        await onChanged?.();
      } else {
        message.error(requestErrorMessage(error));
      }
    } finally {
      if (reconciliationContextRequestRef.current === requestId) {
        setReconciliationLoadingId(null);
      }
    }
  };

  const closeReconciliation = () => {
    if (!submitting) {
      setReconciling(null);
      setReconciliationForm({ ...initialReconciliationForm });
    }
  };

  const submitReconciliation = async () => {
    const operation = reconciling?.operation;
    const context = reconciling?.context;
    const targetSampleNumber = String(
      context?.expected_evidence?.target_sample_number
      || operation?.request_summary?.target_sample_number
      || '',
    );
    if (![
      'confirm_completed',
      'confirm_no_side_effect',
    ].includes(reconciliationForm.action)) {
      message.warning('请先明确选择人工对账结论');
      return;
    }
    if (reconciliationForm.sampleNumber.trim() !== targetSampleNumber) {
      message.warning(
        '请输入完整样品编号 ' + targetSampleNumber + ' 以确认',
      );
      return;
    }
    if (!reconciliationForm.note.trim()) {
      message.warning('请填写对账依据和只读核验说明');
      return;
    }
    const checkedAt = new Date(reconciliationForm.checkedAt);
    if (
      !reconciliationForm.checkedAt
      || Number.isNaN(checkedAt.getTime())
    ) {
      message.warning('请填写只读探针实际完成核验的时间');
      return;
    }

    const expectedEvidence = context?.expected_evidence || {};
    const actionExpected = (
      expectedEvidence?.[reconciliationForm.action] || {}
    );
    const isFinalEntryReconciliation = (
      isFinalEntryReconciliationContext(context)
    );
    let evidence;
    if (isFinalEntryReconciliation) {
      const evidenceContract = (
        expectedEvidence.evidence_contract || context?.evidence_kind
      );
      const expectedExistingRegisterCount = (
        expectedEvidence.expected_existing_register_count
        ?? actionExpected.expected_existing_register_count
      );
      const writerStage = (
        actionExpected.writer_stage || expectedEvidence.writer_stage || ''
      );
      const actualCounts = {
        actual_register_count: reconciliationForm.actualRegisterCount,
        actual_file_reference_count: (
          reconciliationForm.actualFileReferenceCount
        ),
        actual_key_result_count: reconciliationForm.actualKeyResultCount,
        actual_proofed_count: reconciliationForm.actualProofedCount,
      };
      const countsMatch = Object.entries(actualCounts).every(
        ([key, value]) => (
          Number.isInteger(value) && value === actionExpected[key]
        ),
      );
      if (
        evidenceContract !== FINAL_ENTRY_RECONCILIATION_CONTRACT
        || !/^[0-9a-f]{64}$/.test(
          String(expectedEvidence.final_entry_summary_checksum || ''),
        )
        || !Number.isInteger(expectedExistingRegisterCount)
        || !writerStage
        || !countsMatch
        || reconciliationForm.targetFileCount
          !== actionExpected.target_file_count
      ) {
        message.warning(
          '检验记录登记的记录、文件引用、关键结果、校对和目标文件证据尚未与预期闭环',
        );
        return;
      }
      evidence = {
        evidence_contract: evidenceContract,
        checked_at: checkedAt.toISOString(),
        final_entry_summary_checksum: (
          expectedEvidence.final_entry_summary_checksum
        ),
        expected_existing_register_count: expectedExistingRegisterCount,
        ...actualCounts,
        target_file_count: reconciliationForm.targetFileCount,
        writer_stage: writerStage,
      };
      if (reconciliationForm.action === 'confirm_completed') {
        const resultingRegisterCount = actionExpected.resulting_register_count;
        if (
          !Number.isInteger(resultingRegisterCount)
          || !reconciliationForm.remoteRecordId.trim()
        ) {
          message.warning('完整写入结论还缺少新增后的登记记录数或远端记录标识');
          return;
        }
        evidence = {
          ...evidence,
          resulting_register_count: resultingRegisterCount,
          remote_record_id: reconciliationForm.remoteRecordId.trim(),
        };
      }
    } else if (reconciliationForm.action === 'confirm_completed') {
      const commonEvidence = {
        checked_at: checkedAt.toISOString(),
        exact_record_count: reconciliationForm.exactRecordCount,
        target_file_count: reconciliationForm.targetFileCount,
      };
      const expectedSha256 = String(
        expectedEvidence.source_file_sha256 || '',
      ).toLowerCase();
      const remoteSha256 = reconciliationForm.remoteFileSha256
        .trim()
        .toLowerCase();
      if (
        reconciliationForm.exactRecordCount !== 1
        || reconciliationForm.targetFileCount !== 1
        || !reconciliationForm.remoteRecordId.trim()
        || !reconciliationForm.businessFieldsMatch
        || !reconciliationForm.inspectorMatch
        || !expectedSha256
        || remoteSha256 !== expectedSha256
      ) {
        message.warning('完成结论的记录、字段、检验员和文件哈希证据尚未闭环');
        return;
      }
      evidence = {
        ...commonEvidence,
        remote_record_id: reconciliationForm.remoteRecordId.trim(),
        business_fields_match: true,
        inspector_match: true,
        remote_file_sha256: remoteSha256,
      };
    } else {
      const commonEvidence = {
        checked_at: checkedAt.toISOString(),
        exact_record_count: reconciliationForm.exactRecordCount,
        target_file_count: reconciliationForm.targetFileCount,
      };
      if (
        reconciliationForm.exactRecordCount !== 0
        || reconciliationForm.containsRecordCount !== 0
        || reconciliationForm.targetFileCount !== 0
      ) {
        message.warning('只有三项只读计数均为 0 才能确认完全未写入');
        return;
      }
      evidence = {
        ...commonEvidence,
        contains_record_count: reconciliationForm.containsRecordCount,
      };
    }

    setSubmitting(true);
    try {
      await reconcileExecutionExternalOperation(operation.id, {
        action: reconciliationForm.action,
        attempt_id: context.attempt.id,
        payload_checksum: operation.payload_checksum,
        confirmed_sample_number: targetSampleNumber,
        note: reconciliationForm.note.trim(),
        evidence,
      });
      message.success(
        reconciliationForm.action === 'confirm_completed'
          ? '已记录完整写入证据，流程按外部操作成功继续'
          : '已确认完全未写入，本次运行已失败收尾并释放业务围栏',
      );
      setReconciling(null);
      setReconciliationForm({ ...initialReconciliationForm });
      await load();
      await onChanged?.();
    } catch (error) {
      if (shouldRefreshReconciliation(error)) {
        message.warning('待对账操作已变化，正在刷新最新状态');
        setReconciling(null);
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

  const hasReconciliationRequired = operations.some(
    operation => operation.status === 'reconciliation_required',
  );
  const hasUnavailableOperation = operations.some(operation => (
    operation?.request_summary?.safety?.execution_available === false
  ));
  const reconciliationSummary = (
    reconciling?.context?.operation?.request_summary
    || reconciling?.operation?.request_summary
    || {}
  );
  const reconciliationBusiness = reconciliationSummary.business_fields || {};
  const reconciliationFile = Array.isArray(reconciliationSummary.files)
    ? reconciliationSummary.files[0]
    : null;
  const reconciliationBusinessText = [
    reconciliationBusiness.fiber_category,
    reconciliationBusiness.inspection_method,
    reconciliationBusiness.inspection_item,
    reconciliationBusiness.inspection_copies
      ? `${reconciliationBusiness.inspection_copies} 份`
      : null,
  ].filter(Boolean).join(' · ') || '—';
  const reconciliationExpected = (
    reconciling?.context?.expected_evidence || {}
  );
  const isFinalEntryReconciliation = isFinalEntryReconciliationContext(
    reconciling?.context,
  );
  const reconciliationActionExpected = (
    reconciliationExpected?.[reconciliationForm.action] || {}
  );

  return (
    <>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          showIcon
          type={hasReconciliationRequired ? 'error' : hasUnavailableOperation ? 'info' : 'warning'}
          message={
            hasReconciliationRequired
              ? '存在执行结果未知的旧系统操作，禁止重试'
              : hasUnavailableOperation
                ? '当前仅开放预检，不会写入旧系统'
              : '旧系统操作由服务端自动交付'
          }
          description={
            hasReconciliationRequired
              ? '业务围栏仍保持锁定。必须先用只读探针核对远端记录和文件，再由管理员录入明确结论。'
              : hasUnavailableOperation
                ? '相关字段、制品和目标编号仍会形成可核对的预检单；当前部署未开放对应写入能力。'
                : '预检通过后，服务端会直接交付连接器队列；离开当前页面不会中断上传、复核或登记。'
          }
        />
        {operations.map((operation) => {
          const summary = operation.request_summary || {};
          const business = summary.business_fields || {};
          const status = STATUS[operation.status] || {
            label: operation.status || '未知状态',
            color: 'default',
          };
          const files = Array.isArray(summary.files) ? summary.files : [];
          const meta = operationMeta(operation);
          const finalEntry = (
            summary.operation_type === 'legacy_microscopy_check_record_entry'
            && summary.final_entry_summary
          ) || null;
          const paperOperation = isPaperOperation(operation);
          const paperGuidance = paperOperationGuidance(operation);
          const resultContract = summary.result_contract || {};
          const judgementContract = summary.judgement_contract || {};
          const executionAvailable = summary?.safety?.execution_available !== false;
          let cardAction = null;
          if (operation.status === 'prepared' && executionAvailable) {
            cardAction = <Tag color="processing">自动交付中</Tag>;
          } else if (operation.status === 'prepared' && !executionAvailable) {
            cardAction = <Tag color="blue">仅预检</Tag>;
          } else if (operation.status === 'reconciliation_required') {
            cardAction = canReconcile ? (
              <Button
                danger
                loading={reconciliationLoadingId === operation.id}
                disabled={reconciliationLoadingId !== null}
                onClick={() => openReconciliation(operation)}
              >
                录入人工对账结果
              </Button>
            ) : (
              <Text type="danger">请联系管理员处置</Text>
            );
          }
          return (
            <Card
              key={operation.id}
              size="small"
              title={(
                <Space>
                  <span>{meta.title}</span>
                  <Tag color={status.color}>{status.label}</Tag>
                </Space>
              )}
              extra={cardAction}
            >
              {operation.status === 'reconciliation_required' && (
                <Alert
                  style={{ marginBottom: 12 }}
                  showIcon
                  type="error"
                  message="远端副作用未知，本操作不可重领或重试"
                  description="若发现记录或文件只完成一部分、数量不符或哈希不一致，请保持当前状态并继续人工调查。"
                />
              )}
              {paperGuidance && (
                <Alert
                  style={{ marginBottom: 12 }}
                  showIcon
                  type={operation.status === 'prepared' ? 'warning' : 'info'}
                  message={paperGuidance}
                />
              )}
              {finalEntry?.controlled_test && (
                <Alert
                  style={{ marginBottom: 12 }}
                  showIcon
                  type="warning"
                  message={(
                    '受控例外操作：检验记录将从 '
                    + recordCountTransition(finalEntry).replace(' → ', ' 增加到 ')
                  )}
                  description={finalEntry.controlled_test_reason}
                />
              )}
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
                  ...(business.sample_identity ? [{
                    key: 'sample-identity',
                    label: '样品识别',
                    children: business.sample_identity,
                  }] : []),
                  ...(finalEntry ? [{
                    key: 'special-wool-source',
                    label: '来源特纤号',
                    children: (
                      finalEntry.source_review_target_sample_number || '—'
                    ),
                  }, {
                    key: 'register-count-transition',
                    label: '登记记录数',
                    children: recordCountTransition(finalEntry),
                  }] : []),
                  ...(finalEntry?.controlled_test_reason ? [{
                    key: 'controlled-test-reason',
                    label: '受控例外原因',
                    children: (
                      <Text type="danger" strong>
                        {finalEntry.controlled_test_reason}
                      </Text>
                    ),
                  }] : []),
                  {
                    key: 'inspector',
                    label: [
                      'legacy_special_wool_review',
                      'legacy_special_wool_qualitative_review',
                    ].includes(summary.operation_type)
                      ? '复核账号'
                      : '检验员',
                    children: summary.inspector || '—',
                  },
                  ...(summary.target_filename ? [{
                    key: 'target-filename',
                    label: '上传文件名',
                    children: summary.target_filename,
                  }] : []),
                  ...(paperOperation && resultContract.value ? [{
                    key: 'qualitative-result',
                    label: `${resultContract.worksheet || 'Sheet1'}!${resultContract.cell || 'W32'}`,
                    children: (
                      <Space size={6} wrap>
                        <Text strong>{resultContract.value}</Text>
                        {resultContract.unit && (
                          <Tag color="blue">单位：{resultContract.unit}</Tag>
                        )}
                      </Space>
                    ),
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
                    children: (
                      <Text strong>{judgementContract.standard_value || '—'}</Text>
                    ),
                  }] : []),
                  ...(summary.operation_type === 'legacy_generic_check_record_entry'
                    ? [{
                        key: 'proof-policy',
                        label: '登记方式',
                        children: summary.safety?.proof_required === false
                          ? '保存记录，不执行校对'
                          : '保存并按预检单执行后续处理',
                      }]
                    : []),
                  {
                    key: 'fields',
                    label: '固定业务字段',
                    children: [
                      business.fiber_category,
                      business.inspection_method,
                      business.inspection_item,
                      business.sample_identity,
                      business.review_action,
                      business.review_item,
                      business.inspection_copies
                        ? `${business.inspection_copies} 份`
                        : null,
                      business.review_copies
                        ? `${business.review_copies} 份`
                        : null,
                    ].filter(Boolean).join(' · ') || '—',
                  },
                  ...(!executionAvailable ? [{
                    key: 'capability',
                    label: '执行状态',
                    children: (
                      <Alert
                        showIcon
                        type="info"
                        message="已生成预检单，真实写入仍锁定"
                        description={summary.execution_capability?.message}
                      />
                    ),
                  }] : []),
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
                    label: '自动交付时间',
                    children: operation.approval?.approved_at
                      ? dayjs(operation.approval.approved_at).format('YYYY-MM-DD HH:mm:ss')
                      : '等待服务端交付',
                  },
                  {
                    key: 'expires',
                    label: operation.approval?.approved_at
                      ? '交付时限'
                      : '预检有效至',
                    children: operation.approval?.approved_at
                      ? (
                        operation.approval?.expires_at
                          ? dayjs(operation.approval.expires_at).format('YYYY-MM-DD HH:mm:ss')
                          : '服务端自动交付，不设倒计时'
                      )
                      : (
                        operation.preflight_expires_at
                          ? dayjs(operation.preflight_expires_at).format('YYYY-MM-DD HH:mm:ss')
                          : '—'
                      ),
                  },
                ]}
              />
              {files.length > 0 && (
                <Table
                  size="small"
                  pagination={false}
                  rowKey={row => row.id || row.artifact_id}
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
              )}
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
        width={680}
        title="录入旧系统人工对账结论"
        open={Boolean(reconciling)}
        okText={
          reconciliationForm.action === 'confirm_completed'
            ? isFinalEntryReconciliation
              ? '确认新增记录完整并继续流程'
              : '确认完整写入并继续流程'
            : reconciliationForm.action === 'confirm_no_side_effect'
              ? isFinalEntryReconciliation
                ? '确认未产生新增写入并失败收尾'
                : '确认完全未写入并失败收尾'
              : '请先选择对账结论'
        }
        okButtonProps={{
          danger: Boolean(reconciliationForm.action),
          disabled: !reconciliationForm.action,
        }}
        cancelText="保持锁定"
        confirmLoading={submitting}
        onCancel={closeReconciliation}
        onOk={submitReconciliation}
        destroyOnHidden
      >
        <Alert
          showIcon
          type="error"
          message={isFinalEntryReconciliation
            ? '这里只接受新增记录完整成功或确认未产生任何新增写入；部分成功必须继续保持锁定'
            : '这里只接受完整成功或完全未写入；部分成功必须继续保持锁定'}
          description={
            reconciling?.context?.attestation_notice
            || '这里记录的是管理员基于只读核对结果作出的人工声明；系统不会自动执行或签名远端探针。'
          }
        />
        {isFinalEntryReconciliation && (
          <Descriptions
            style={{ marginTop: 16 }}
            size="small"
            column={1}
            items={[
              {
                key: 'evidence-contract',
                label: '对账证据契约',
                children: '微观形貌检验记录登记 v1',
              },
              {
                key: 'existing-register-count',
                label: '原有登记记录数',
                children: reconciliationExpected
                  .expected_existing_register_count ?? '—',
              },
              {
                key: 'writer-stage',
                label: '写入器失败阶段',
                children: reconciliationExpected.writer_stage || '—',
              },
              {
                key: 'summary-checksum',
                label: '预检摘要校验和',
                children: (
                  <Text code>
                    {reconciliationExpected.final_entry_summary_checksum
                      || '—'}
                  </Text>
                ),
              },
            ]}
          />
        )}
        <Descriptions
          style={{ marginTop: 16 }}
          size="small"
          column={1}
          items={[
            {
              key: 'attempt',
              label: '失败尝试',
              children: reconciling?.context?.attempt
                ? (
                  '第 '
                  + reconciling.context.attempt.attempt_no
                  + ' 次 · 最远阶段 '
                  + (reconciling.context.attempt.current_stage || '未知')
                )
                : '—',
            },
            {
              key: 'error',
              label: '失败原因',
              children: (
                reconciling?.context?.attempt?.error?.message
                || reconciling?.context?.attempt?.error?.code
                || '—'
              ),
            },
            {
              key: 'target',
              label: '目标样品编号',
              children: reconciliationSummary.target_sample_number || '—',
            },
            {
              key: 'source',
              label: '源检验编号',
              children: reconciliationSummary.source_inspection_number || '—',
            },
            {
              key: 'inspector',
              label: '预期检验员',
              children: reconciliationSummary.inspector || '—',
            },
            {
              key: 'business',
              label: '预期固定业务字段',
              children: reconciliationBusinessText,
            },
            {
              key: 'file',
              label: '预期目标文件',
              children: reconciliationFile?.filename || '—',
            },
            {
              key: 'sha256',
              label: '预期文件 SHA-256',
              children: (
                <Text code>
                  {reconciling?.context?.expected_evidence
                    ?.source_file_sha256 || '—'}
                </Text>
              ),
            },
          ]}
        />

        <Paragraph style={{ marginTop: 16, marginBottom: 6 }}>
          对账结论
        </Paragraph>
        <Radio.Group
          aria-label="对账结论"
          value={reconciliationForm.action}
          onChange={(event) => {
            setReconciliationForm(current => ({
              ...initialReconciliationForm,
              action: event.target.value,
              checkedAt: current.checkedAt,
              sampleNumber: current.sampleNumber,
              note: current.note,
            }));
          }}
        >
          <Space direction="vertical">
            <Radio value="confirm_completed">
              {isFinalEntryReconciliation
                ? '已确认新增登记记录、文件和校对均完整'
                : '已确认记录和文件均完整写入'}
            </Radio>
            <Radio value="confirm_no_side_effect">
              {isFinalEntryReconciliation
                ? '已确认原有记录未变且没有产生新增写入'
                : '已确认记录和文件均完全不存在'}
            </Radio>
          </Space>
        </Radio.Group>

        <Space
          direction="vertical"
          size={6}
          style={{ width: '100%', marginTop: 16 }}
        >
          <Text>只读探针实际完成核验的时间（必填）</Text>
          <Input
            aria-label="只读核验时间"
            type="datetime-local"
            step="1"
            value={reconciliationForm.checkedAt}
            onChange={event => setReconciliationForm(current => ({
              ...current,
              checkedAt: event.target.value,
            }))}
          />
          <Text type="secondary">
            请填写探针报告中的核验时间；系统会据此校验证据是否仍然有效。
          </Text>
        </Space>

        {isFinalEntryReconciliation && reconciliationForm.action ? (
          <Space
            direction="vertical"
            size={10}
            style={{ width: '100%', marginTop: 16 }}
          >
            <Alert
              showIcon
              type="warning"
              message={reconciliationForm.action === 'confirm_completed'
                ? '必须证明新增后的登记、文件引用、关键结果和校对数量全部达到预期'
                : '必须证明原有登记、文件引用、关键结果和校对数量均未变化，且目标文件不存在'}
              description={(
                '写入器阶段：'
                + (reconciliationActionExpected.writer_stage || '—')
                + '；允许判定此结论的阶段：'
                + (
                  Array.isArray(
                    reconciliationActionExpected.allowed_writer_stages,
                  )
                    ? reconciliationActionExpected.allowed_writer_stages
                      .join('、')
                    : '—'
                )
              )}
            />
            <Text>
              实际登记记录数（预期为
              {' '}
              {reconciliationActionExpected.actual_register_count ?? '—'}
              ）
            </Text>
            <InputNumber
              aria-label="实际登记记录数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.actualRegisterCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                actualRegisterCount: value,
              }))}
            />
            <Text>
              实际文件引用数（预期为
              {' '}
              {reconciliationActionExpected.actual_file_reference_count
                ?? '—'}
              ）
            </Text>
            <InputNumber
              aria-label="实际文件引用数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.actualFileReferenceCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                actualFileReferenceCount: value,
              }))}
            />
            <Text>
              实际关键结果数（预期为
              {' '}
              {reconciliationActionExpected.actual_key_result_count ?? '—'}
              ）
            </Text>
            <InputNumber
              aria-label="实际关键结果数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.actualKeyResultCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                actualKeyResultCount: value,
              }))}
            />
            <Text>
              实际已校对记录数（预期为
              {' '}
              {reconciliationActionExpected.actual_proofed_count ?? '—'}
              ）
            </Text>
            <InputNumber
              aria-label="实际已校对记录数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.actualProofedCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                actualProofedCount: value,
              }))}
            />
            <Text>
              目标文件数（预期为
              {' '}
              {reconciliationActionExpected.target_file_count ?? '—'}
              ）
            </Text>
            <InputNumber
              aria-label="目标文件数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.targetFileCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                targetFileCount: value,
              }))}
            />
            {reconciliationForm.action === 'confirm_completed' && (
              <>
                <Text>
                  新增后登记记录数（固定为
                  {' '}
                  {reconciliationActionExpected.resulting_register_count
                    ?? '—'}
                  ）
                </Text>
                <Input
                  aria-label="远端记录标识"
                  autoComplete="off"
                  maxLength={200}
                  placeholder="请输入新增记录的远端标识"
                  value={reconciliationForm.remoteRecordId}
                  onChange={event => setReconciliationForm(current => ({
                    ...current,
                    remoteRecordId: event.target.value,
                  }))}
                />
              </>
            )}
          </Space>
        ) : reconciliationForm.action === 'confirm_completed' ? (
          <Space
            direction="vertical"
            size={10}
            style={{ width: '100%', marginTop: 16 }}
          >
            <Alert
              showIcon
              type="warning"
              message="完成证据必须同时满足：精确记录唯一、字段和检验员一致、目标文件唯一且哈希一致"
            />
            <Text>目标精确记录数（必须为 1）</Text>
            <InputNumber
              aria-label="目标精确记录数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.exactRecordCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                exactRecordCount: value,
              }))}
            />
            <Text>目标文件数（必须为 1）</Text>
            <InputNumber
              aria-label="目标文件数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.targetFileCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                targetFileCount: value,
              }))}
            />
            <Text>远端记录标识</Text>
            <Input
              aria-label="远端记录标识"
              autoComplete="off"
              maxLength={200}
              value={reconciliationForm.remoteRecordId}
              onChange={event => setReconciliationForm(current => ({
                ...current,
                remoteRecordId: event.target.value,
              }))}
            />
            <Checkbox
              checked={reconciliationForm.businessFieldsMatch}
              onChange={event => setReconciliationForm(current => ({
                ...current,
                businessFieldsMatch: event.target.checked,
              }))}
            >
              只读回查的固定业务字段与预检单完全一致
            </Checkbox>
            <Checkbox
              checked={reconciliationForm.inspectorMatch}
              onChange={event => setReconciliationForm(current => ({
                ...current,
                inspectorMatch: event.target.checked,
              }))}
            >
              只读回查的检验员与预检单完全一致
            </Checkbox>
            <Text>
              远端文件 SHA-256（必须等于
              {' '}
              <Text code>
                {reconciling?.context?.expected_evidence?.source_file_sha256 || '—'}
              </Text>
              ）
            </Text>
            <Input
              aria-label="远端文件 SHA-256"
              autoComplete="off"
              maxLength={64}
              value={reconciliationForm.remoteFileSha256}
              onChange={event => setReconciliationForm(current => ({
                ...current,
                remoteFileSha256: event.target.value,
              }))}
            />
          </Space>
        ) : reconciliationForm.action === 'confirm_no_side_effect' ? (
          <Space
            direction="vertical"
            size={10}
            style={{ width: '100%', marginTop: 16 }}
          >
            <Alert
              showIcon
              type="warning"
              message="完全未写入必须由三项独立只读计数均为 0 证明"
            />
            <Text>目标精确记录数（必须为 0）</Text>
            <InputNumber
              aria-label="目标精确记录数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.exactRecordCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                exactRecordCount: value,
              }))}
            />
            <Text>包含匹配记录数（必须为 0）</Text>
            <InputNumber
              aria-label="包含匹配记录数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.containsRecordCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                containsRecordCount: value,
              }))}
            />
            <Text>目标文件数（必须为 0）</Text>
            <InputNumber
              aria-label="目标文件数"
              min={0}
              precision={0}
              style={{ width: '100%' }}
              value={reconciliationForm.targetFileCount}
              onChange={value => setReconciliationForm(current => ({
                ...current,
                targetFileCount: value,
              }))}
            />
          </Space>
        ) : null}

        <Paragraph style={{ marginTop: 16, marginBottom: 6 }}>
          请输入完整目标样品编号
          <Text strong>
            {' '}
            {reconciling?.context?.expected_evidence?.target_sample_number}
            {' '}
          </Text>
          以确认
        </Paragraph>
        <Input
          aria-label="对账确认样品编号"
          autoComplete="off"
          value={reconciliationForm.sampleNumber}
          onChange={event => setReconciliationForm(current => ({
            ...current,
            sampleNumber: event.target.value,
          }))}
        />
        <Paragraph style={{ marginTop: 12, marginBottom: 6 }}>
          对账依据和只读核验说明（必填）
        </Paragraph>
        <Input.TextArea
          aria-label="对账依据"
          rows={3}
          maxLength={2000}
          value={reconciliationForm.note}
          onChange={event => setReconciliationForm(current => ({
            ...current,
            note: event.target.value,
          }))}
        />
      </Modal>
    </>
  );
}
