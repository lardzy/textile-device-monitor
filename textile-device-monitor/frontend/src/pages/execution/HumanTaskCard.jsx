import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Collapse,
  Descriptions,
  Divider,
  Form,
  Modal,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  claimHumanTask,
  getExecutionRunMutation,
  rejectHumanTask,
  saveHumanTaskDraft,
  submitHumanTask,
} from '../../api/execution';
import { useExecutionAuth } from './ExecutionAuthContext';
import SchemaFields from './SchemaFields';

const { Paragraph, Text } = Typography;

const candidateId = candidate => (
  typeof candidate === 'string' ? candidate : candidate?.id
);

const artifactLabel = value => (
  value
    ? `${value.root_id || value.rootId || '—'} / ${value.relative_path || value.relativePath || '—'}`
    : '—'
);

const requestErrorMessage = error => (
  error?.requestId
    ? `${error.message || '人工任务操作失败'}（请求编号：${error.requestId}）`
    : error?.message || '人工任务操作失败'
);

export default function HumanTaskCard({ task, nodeRun, onChanged }) {
  const { user } = useExecutionAuth();
  const [form] = Form.useForm();
  const [working, setWorking] = useState(false);
  const [approvalMutation, setApprovalMutation] = useState(null);
  const [approvalMutationError, setApprovalMutationError] = useState(null);
  const [approvalMutationLoading, setApprovalMutationLoading] = useState(false);
  const [approvalMutationReloadKey, setApprovalMutationReloadKey] = useState(0);
  const claimedById = task.claimed_by_id || task.claimed_by?.id || task.assignee?.id;
  const isClaimed = Boolean(claimedById || task.status === 'claimed');
  const isClaimedByMe = Boolean(claimedById && String(claimedById) === String(user?.id));
  const isClosed = !['open', 'pending', 'claimed'].includes(task.status);
  const schema = task.form_schema || task.schema || {
    type: 'object',
    properties: {},
  };
  const formSchema = useMemo(() => {
    const properties = { ...(schema.properties || {}) };
    delete properties.selected_files;
    return { ...schema, properties };
  }, [schema]);
  const candidatePayload = nodeRun?.input_data?.candidates
    || nodeRun?.input_data?.groups
    || nodeRun?.input_data
    || {};
  const allCandidates = Array.isArray(candidatePayload)
    ? candidatePayload
    : candidatePayload.candidates
      || candidatePayload.items
      || candidatePayload.groups
      || [];
  const candidates = allCandidates.filter(candidate => Boolean(candidateId(candidate)));
  const ignoredCandidateCount = allCandidates.length - candidates.length;
  const approvalContext = nodeRun?.input_data?.approval_context;
  const approvalMutationId = approvalContext?.mutation_id;

  const valuesForSubmit = () => {
    const values = form.getFieldsValue();
    if (!Array.isArray(values.selected_files)) {
      return values;
    }
    return {
      ...values,
      // 客户端只提交服务端签发的稳定 ID；路径、文件成员和指纹由服务端重新核对。
      selected_files: values.selected_files.map(value => String(value)),
    };
  };

  useEffect(() => {
    const values = task.draft_data || task.draft_values || task.values || {};
    form.setFieldsValue({
      ...values,
      selected_files: Array.isArray(values.selected_files)
        ? values.selected_files.map(candidateId).filter(Boolean).map(String)
        : values.selected_files,
    });
  }, [form, task]);

  useEffect(() => {
    if (!approvalMutationId || !task.run_id || !isClaimedByMe || isClosed) {
      setApprovalMutation(null);
      setApprovalMutationError(null);
      setApprovalMutationLoading(false);
      return undefined;
    }

    let cancelled = false;
    setApprovalMutation(null);
    setApprovalMutationError(null);
    setApprovalMutationLoading(true);
    getExecutionRunMutation(task.run_id, approvalMutationId)
      .then((payload) => {
        if (!cancelled) {
          setApprovalMutation(payload?.mutation || payload);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setApprovalMutationError(error);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setApprovalMutationLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    approvalMutationId,
    approvalMutationReloadKey,
    isClaimedByMe,
    isClosed,
    task.run_id,
  ]);

  const execute = async (action) => {
    setWorking(true);
    try {
      if (action === 'claim') {
        await claimHumanTask(task.id, task.revision);
      } else if (action === 'save') {
        await saveHumanTaskDraft(task.id, task.revision, valuesForSubmit());
      } else if (action === 'submit') {
        await form.validateFields();
        await submitHumanTask(task.id, task.revision, valuesForSubmit());
      } else if (action === 'reject') {
        const reason = await new Promise((resolve, reject) => {
          let value = '';
          Modal.confirm({
            title: '驳回此人工任务？',
            content: (
              <Form.Item label="原因" style={{ marginTop: 16, marginBottom: 0 }}>
                <input
                  className="ant-input"
                  onChange={event => { value = event.target.value; }}
                  placeholder="请输入驳回原因"
                />
              </Form.Item>
            ),
            okText: '确认驳回',
            okButtonProps: { danger: true },
            cancelText: '取消',
            onOk: () => value.trim()
              ? resolve(value.trim())
              : Promise.reject(new Error('请输入驳回原因')),
            onCancel: () => reject(new Error('cancelled')),
          });
        });
        await rejectHumanTask(task.id, task.revision, reason);
      }
      message.success(action === 'submit' ? '任务已提交' : '操作已保存');
      await onChanged?.();
    } catch (error) {
      if (error.message !== 'cancelled') {
        if (error.status === 409 || error.status === 404) {
          message.warning(error.status === 409
            ? '任务已被其他人员更新，正在刷新最新状态'
            : '任务已结束或不再由当前账号处理');
          await onChanged?.();
        } else {
          message.error(requestErrorMessage(error));
        }
      }
    } finally {
      setWorking(false);
    }
  };

  return (
    <Card
      size="small"
      className="execution-human-task"
      title={task.title || task.name || '人工处理'}
      extra={(
        <Tag color={task.status === 'claimed' ? 'processing' : 'orange'}>
          {task.status === 'claimed' ? '处理中' : '待领取'}
        </Tag>
      )}
    >
      <Form form={form} layout="vertical">
        <Paragraph type="secondary">
          {task.description || '请核对当前步骤并完成所需输入。'}
        </Paragraph>
        {ignoredCandidateCount > 0 && (
          <Alert
            showIcon
            type="warning"
            message={`${ignoredCandidateCount} 项候选资料缺少稳定 ID，已禁止选择`}
            description="请刷新任务；客户端不会使用路径拼接临时候选标识。"
            style={{ marginBottom: 12 }}
          />
        )}
        {approvalContext && (
          <section className="execution-human-task__approval">
            <Alert
              showIcon
              type="warning"
              message="请先核对以下发布上下文，再确认发布"
              description="校验和用于绑定本次工作副本、变更计划与发布目标；任一内容变化后，服务器都会拒绝旧确认。"
            />
            <Descriptions size="small" bordered column={1}>
              <Descriptions.Item label="变更编号">
                {approvalContext.mutation_id || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="工作副本">
                {artifactLabel(approvalContext.working_copy)}
              </Descriptions.Item>
              <Descriptions.Item label="工作副本校验和">
                <Text code copyable>
                  {approvalContext.working_copy?.content_sha256 || '—'}
                </Text>
              </Descriptions.Item>
              <Descriptions.Item label="发布目标">
                {artifactLabel(approvalContext.target)}
              </Descriptions.Item>
              <Descriptions.Item label="变更计划校验和">
                <Text code copyable>{approvalContext.change_plan_checksum || '—'}</Text>
              </Descriptions.Item>
            </Descriptions>
            {isClaimedByMe && !isClosed && (
              <div className="execution-human-task__mutation-detail">
                {approvalMutationLoading ? (
                  <div className="execution-human-task__mutation-loading">
                    <Spin size="small" />
                    <Text type="secondary">正在读取服务器保存的变更清单…</Text>
                  </div>
                ) : approvalMutationError ? (
                  <Alert
                    showIcon
                    type="error"
                    message="无法读取变更清单，暂不能确认发布"
                    description={requestErrorMessage(approvalMutationError)}
                    action={(
                      <Button
                        size="small"
                        onClick={() => setApprovalMutationReloadKey(value => value + 1)}
                      >
                        重试
                      </Button>
                    )}
                  />
                ) : approvalMutation ? (
                  <Collapse
                    size="small"
                    items={[
                      {
                        key: 'change-plan',
                        label: '查看完整变更预检清单',
                        children: (
                          <pre className="execution-mutation-json">
                            {JSON.stringify(approvalMutation.change_plan || {}, null, 2)}
                          </pre>
                        ),
                      },
                      {
                        key: 'verification',
                        label: '查看保存后重读核对结果',
                        children: (
                          <pre className="execution-mutation-json">
                            {JSON.stringify(approvalMutation.verification_result || {}, null, 2)}
                          </pre>
                        ),
                      },
                    ]}
                  />
                ) : null}
              </div>
            )}
            <Divider />
          </section>
        )}
        {isClosed ? (
          <Alert
            showIcon
            type={task.status === 'completed' ? 'success' : 'info'}
            message={task.status === 'completed' ? '该任务已完成' : '该任务已经结束'}
            description={task.completed_at
              ? `完成时间：${new Date(task.completed_at).toLocaleString()}`
              : '任务已不可再次领取或提交。'}
          />
        ) : !isClaimed ? (
          <Button type="primary" block loading={working} onClick={() => execute('claim')}>
            领取并处理
          </Button>
        ) : !isClaimedByMe ? (
          <Alert
            showIcon
            type="info"
            message="该任务已由其他人员领取"
            description="任务提交后收件箱会自动更新，请勿在多个账号之间重复处理。"
          />
        ) : (
          <>
          {candidates.length > 0 && (
            <Form.Item
              name="selected_files"
              label={`候选资料（${candidates.length}）`}
              rules={[{ required: true, message: '请至少选择一项资料' }]}
            >
              <Checkbox.Group className="execution-candidate-list">
                {candidates.map((candidate) => {
                  const path = candidate.relative_path
                    || candidate.key
                    || candidate.base_name
                    || candidate.name;
                  const fileCount = Array.isArray(candidate.files)
                    ? `${candidate.files.length} 个关联文件`
                    : candidate.suffix;
                  return (
                    <Checkbox key={candidateId(candidate)} value={String(candidateId(candidate))}>
                      <span className="execution-candidate-list__item">
                        <strong>{candidate.name || candidate.base_name || path}</strong>
                        <small>{path}{fileCount ? ` · ${fileCount}` : ''}</small>
                      </span>
                    </Checkbox>
                  );
                })}
              </Checkbox.Group>
            </Form.Item>
          )}
          <SchemaFields schema={formSchema} />
          <Space wrap>
            <Button loading={working} onClick={() => execute('save')}>保存草稿</Button>
            <Button
              type="primary"
              loading={working}
              disabled={Boolean(approvalContext) && !approvalMutation}
              onClick={() => execute('submit')}
            >
              确认提交
            </Button>
            <Button danger loading={working} onClick={() => execute('reject')}>驳回</Button>
          </Space>
          </>
        )}
      </Form>
    </Card>
  );
}
