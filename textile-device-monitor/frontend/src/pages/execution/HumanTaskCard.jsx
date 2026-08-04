import { useEffect, useMemo, useRef, useState } from 'react';
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
import ExecutionImageSelector, {
  imageSelectionFolderId,
  imageSelectionImageId,
} from './ExecutionImageSelector';
import ExecutionResultFiles, { resultFileId } from './ExecutionResultFiles';
import MicroscopyRecordHumanTask, {
  microscopyTaskKind,
} from './MicroscopyRecordHumanTask';
import SchemaFields from './SchemaFields';

const { Paragraph, Text } = Typography;

const candidateId = candidate => (
  typeof candidate === 'string' ? candidate : resultFileId(candidate)
);

const SilentFormField = () => null;

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

const taskConditionLabels = {
  task_item_name: '检测项目名称',
  test_method: '测试方法',
};

export default function HumanTaskCard({ task, nodeRun, onChanged }) {
  const { user } = useExecutionAuth();
  const [form] = Form.useForm();
  const selectedFiles = Form.useWatch('selected_files', form) || [];
  const primaryFileId = Form.useWatch('primary_file_id', form) || null;
  const selectedFolderIds = Form.useWatch('selected_folder_ids', form) || [];
  const selectedImageIds = Form.useWatch('selected_image_ids', form) || [];
  const [working, setWorking] = useState(false);
  const [approvalMutation, setApprovalMutation] = useState(null);
  const [approvalMutationError, setApprovalMutationError] = useState(null);
  const [approvalMutationLoading, setApprovalMutationLoading] = useState(false);
  const [approvalMutationReloadKey, setApprovalMutationReloadKey] = useState(0);
  const formSyncRef = useRef(null);
  const claimedById = task.claimed_by_id || task.claimed_by?.id || task.assignee?.id;
  const isClaimed = Boolean(claimedById || task.status === 'claimed');
  const isClaimedByMe = Boolean(claimedById && String(claimedById) === String(user?.id));
  const isClosed = !['open', 'pending', 'claimed'].includes(task.status);
  const schema = task.form_schema || task.schema || {
    type: 'object',
    properties: {},
  };
  const taskKind = microscopyTaskKind(task, nodeRun);
  const hasMicroscopyTask = [
    'microscopy_record_input',
    'microscopy_print_confirmation',
  ].includes(taskKind);
  const formSchema = useMemo(() => {
    const properties = { ...(schema.properties || {}) };
    delete properties.selected_files;
    delete properties.primary_file_id;
    delete properties.selected_folder_ids;
    delete properties.selected_image_ids;
    delete properties.primary_image_id;
    if (hasMicroscopyTask) {
      delete properties.selected_project_key;
      delete properties.sample_name;
      delete properties.sample_identity;
      delete properties.judge_basis;
      delete properties.judgement;
      delete properties.artifact_sha256;
      delete properties.printed;
    }
    return { ...schema, properties };
  }, [hasMicroscopyTask, schema]);
  const candidatePayload = nodeRun?.input_data?.files
    || nodeRun?.input_data?.result_files
    || nodeRun?.input_data?.results
    || nodeRun?.input_data?.candidates
    || nodeRun?.input_data?.groups
    || nodeRun?.input_data
    || {};
  const allCandidates = Array.isArray(candidatePayload)
    ? candidatePayload
    : candidatePayload.files
      || candidatePayload.result_files
      || candidatePayload.results
      || candidatePayload.candidates
      || candidatePayload.items
      || candidatePayload.groups
      || [];
  const candidates = allCandidates.filter(candidate => Boolean(candidateId(candidate)));
  const hasImageSelection = Object.hasOwn(candidatePayload, 'folders')
    || Object.hasOwn(candidatePayload, 'images');
  const imageFolders = Array.isArray(candidatePayload.folders)
    ? candidatePayload.folders
    : [];
  const imageCandidates = Array.isArray(candidatePayload.images)
    ? candidatePayload.images
    : [];
  const folderSelectionRequired = Boolean(candidatePayload.folder_selection_required);
  const imageListTruncated = Boolean(candidatePayload.truncated);
  const taskValidationState = candidatePayload.task_validation_state;
  const missingTaskConditions = Array.isArray(candidatePayload.missing_conditions)
    ? candidatePayload.missing_conditions.filter(value => taskConditionLabels[value])
    : [];
  const ignoredCandidateCount = allCandidates.length - candidates.length;
  const hasResultDetails = candidates.some(candidate => (
    candidate?.result
    || Array.isArray(candidate?.parts)
    || Array.isArray(candidate?.remarks)
    || Array.isArray(candidate?.images)
    || candidate?.read_status
  ));
  const approvalContext = nodeRun?.input_data?.approval_context;
  const approvalMutationId = approvalContext?.mutation_id;

  const valuesForSubmit = () => {
    const values = form.getFieldsValue();
    if (hasImageSelection) {
      const normalizedImageIds = Array.isArray(values.selected_image_ids)
        ? values.selected_image_ids.map(value => String(value))
        : [];
      return {
        ...values,
        selected_folder_ids: Array.isArray(values.selected_folder_ids)
          ? values.selected_folder_ids.map(value => String(value))
          : [],
        selected_image_ids: normalizedImageIds,
        primary_image_id: values.primary_image_id
          ? String(values.primary_image_id)
          : normalizedImageIds[0],
      };
    }
    if (!Array.isArray(values.selected_files)) {
      return values;
    }
    return {
      ...values,
      // 客户端只提交服务端签发的稳定 ID；路径、文件成员和指纹由服务端重新核对。
      selected_files: values.selected_files.map(value => String(value)),
      ...(values.primary_file_id
        ? { primary_file_id: String(values.primary_file_id) }
        : {}),
    };
  };

  useEffect(() => {
    const syncIdentity = {
      taskId: String(task.id),
      revision: task.revision ?? null,
    };
    const previous = formSyncRef.current;
    if (
      previous
      && previous.taskId === syncIdentity.taskId
      && previous.revision === syncIdentity.revision
    ) {
      // SSE 重连和普通快照刷新会创建新的 task 对象。只要服务端
      // revision 没有变化，就保留尚未保存的本地选单和主单。
      return;
    }
    const values = task.draft_data || task.draft_values || task.values || {};
    if (previous && previous.taskId !== syncIdentity.taskId) {
      // 同一个卡片实例切换到另一项任务时才需要完整清空。首次挂载或
      // 同任务 revision 更新直接写入服务端值，避免 resetFields 触发
      // 表单子项重挂载并吞掉用户刚完成的选择。
      form.resetFields();
    }
    form.setFieldsValue({
      ...values,
      selected_files: Array.isArray(values.selected_files)
        ? values.selected_files.map(candidateId).filter(Boolean).map(String)
        : candidates.filter(candidate => candidate.selected)
          .map(candidateId)
          .filter(Boolean)
          .map(String),
      primary_file_id: candidateId(values.primary_file)
        || values.primary_file_id
        || candidateId(candidates.find(candidate => candidate.is_primary))
        || undefined,
      selected_folder_ids: Array.isArray(values.selected_folder_ids)
        ? values.selected_folder_ids.map(imageSelectionFolderId).filter(Boolean).map(String)
        : (Array.isArray(candidatePayload.selected_folder_ids)
          ? candidatePayload.selected_folder_ids
          : imageFolders.filter(folder => folder.selected))
          .map(imageSelectionFolderId)
          .filter(Boolean)
          .map(String),
      selected_image_ids: Array.isArray(values.selected_image_ids)
        ? values.selected_image_ids.map(imageSelectionImageId).filter(Boolean).map(String)
        : imageCandidates.filter(image => image.selected)
          .map(imageSelectionImageId)
          .filter(Boolean)
          .map(String),
      primary_image_id: imageSelectionImageId(values.primary_image)
        || values.primary_image_id
        || imageSelectionImageId(imageCandidates.find(image => image.is_primary))
        || undefined,
    });
    formSyncRef.current = syncIdentity;
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
        if (Array.isArray(error?.errorFields)) {
          // Ant Design 已将本地表单校验错误显示在对应字段附近。
          // 这里不再把它误报成接口操作失败。
        } else if (error.status === 409 || error.status === 404) {
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
          {hasImageSelection && (
            <Form.Item label="图片结果选择" required>
              {imageListTruncated && (
                <Alert
                  showIcon
                  type="warning"
                  message="候选图片数量超过展示上限"
                  description="当前仅展示最近的 2000 张图片，请核对检验编号或整理目录后刷新索引。"
                  style={{ marginBottom: 12 }}
                />
              )}
              {taskValidationState === 'pending' && (
                <Alert
                  showIcon
                  type="info"
                  message="旧系统任务信息正在后台刷新"
                  description="当前可先根据编号文件夹选择图片；刷新完成后，流程推荐会自动更新。"
                  style={{ marginBottom: 12 }}
                />
              )}
              {taskValidationState === 'warning' && missingTaskConditions.length > 0 && (
                <Alert
                  showIcon
                  type="warning"
                  message="旧系统任务条件未完全匹配"
                  description={`未匹配：${missingTaskConditions
                    .map(value => taskConditionLabels[value])
                    .join('、')}。本轮仍允许人工核对并选择图片。`}
                  style={{ marginBottom: 12 }}
                />
              )}
              {folderSelectionRequired && (
                <Form.Item
                  name="selected_folder_ids"
                  noStyle
                  rules={[{
                    validator: (_, value) => (
                      Array.isArray(value) && value.length > 0
                        ? Promise.resolve()
                        : Promise.reject(new Error('请至少选择一个结果文件夹'))
                    ),
                  }]}
                >
                  <SilentFormField />
                </Form.Item>
              )}
              <Form.Item
                name="selected_image_ids"
                noStyle
                rules={[{
                  validator: (_, value) => {
                    if (!Array.isArray(value) || value.length === 0) {
                      return Promise.reject(new Error('请至少选择一张结果图片'));
                    }
                    if (value.length > 10) {
                      return Promise.reject(new Error('最多选择 10 张结果图片'));
                    }
                    return Promise.resolve();
                  },
                }]}
              >
                <SilentFormField />
              </Form.Item>
              <Form.Item name="primary_image_id" noStyle>
                <SilentFormField />
              </Form.Item>
              <ExecutionImageSelector
                folders={imageFolders}
                images={imageCandidates}
                disabled={working}
                selectedFolderIds={Array.isArray(selectedFolderIds) ? selectedFolderIds : []}
                selectedImageIds={Array.isArray(selectedImageIds) ? selectedImageIds : []}
                onSelectedFolderIdsChange={(value) => {
                  form.setFieldValue('selected_folder_ids', value);
                  form.validateFields(['selected_folder_ids']).catch(() => {});
                }}
                onSelectedImageIdsChange={(value) => {
                  form.setFieldValue('selected_image_ids', value);
                  form.validateFields(['selected_image_ids']).catch(() => {});
                }}
                onPrimaryImageIdChange={(value) => {
                  form.setFieldValue('primary_image_id', value || undefined);
                }}
              />
              <Form.Item noStyle shouldUpdate>
                {() => {
                  const errors = [
                    ...form.getFieldError('selected_folder_ids'),
                    ...form.getFieldError('selected_image_ids'),
                  ];
                  return errors.length ? (
                    <div className="execution-result-file-selection__error" role="alert">
                      {errors[0]}
                    </div>
                  ) : null;
                }}
              </Form.Item>
            </Form.Item>
          )}
          {!hasImageSelection && candidates.length > 0 && (hasResultDetails ? (
            <Form.Item label={`文件读取结果（${candidates.length}）`} required>
              <Form.Item
                name="selected_files"
                noStyle
                rules={[{
                  validator: (_, value) => (
                    Array.isArray(value) && value.length
                      ? Promise.resolve()
                      : Promise.reject(new Error('请至少选择一项需要的文件'))
                  ),
                }]}
              >
                <SilentFormField />
              </Form.Item>
              <Form.Item
                name="primary_file_id"
                noStyle
                dependencies={['selected_files']}
                rules={[{
                  validator: (_, value) => {
                    const selected = form.getFieldValue('selected_files') || [];
                    return value && selected.map(String).includes(String(value))
                      ? Promise.resolve()
                      : Promise.reject(new Error('请从需要的文件中指定一个主单'));
                  },
                }]}
              >
                <SilentFormField />
              </Form.Item>
              <ExecutionResultFiles
                files={candidates}
                selectable
                disabled={working}
                selectedIds={Array.isArray(selectedFiles) ? selectedFiles : []}
                primaryId={primaryFileId}
                onSelectedIdsChange={(value) => {
                  form.setFieldValue('selected_files', value);
                  form.validateFields(['selected_files']).catch(() => {});
                }}
                onPrimaryIdChange={(value) => {
                  form.setFieldValue('primary_file_id', value || undefined);
                  form.validateFields(['primary_file_id']).catch(() => {});
                }}
              />
              <Form.Item noStyle shouldUpdate>
                {() => {
                  const errors = [
                    ...form.getFieldError('selected_files'),
                    ...form.getFieldError('primary_file_id'),
                  ];
                  return errors.length ? (
                    <div className="execution-result-file-selection__error" role="alert">
                      {errors[0]}
                    </div>
                  ) : null;
                }}
              </Form.Item>
            </Form.Item>
          ) : (
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
                  const displayName = candidate.name
                    || candidate.base_name
                    || String(path).split(/[\\/]/).pop();
                  const pathParts = String(path || '').split(/[\\/]/);
                  const parentPath = pathParts.length > 1
                    ? pathParts.slice(0, -1).join('/')
                    : '根目录';
                  const fileCount = Array.isArray(candidate.files)
                    ? `${candidate.files.length} 个关联文件`
                    : candidate.suffix;
                  const secondaryText = [parentPath, fileCount]
                    .filter(Boolean)
                    .join(' · ');
                  return (
                    <Checkbox key={candidateId(candidate)} value={String(candidateId(candidate))}>
                      <span
                        className="execution-candidate-list__item"
                        title={String(path || displayName)}
                      >
                        <strong title={displayName}>{displayName}</strong>
                        <small title={`${path}${fileCount ? ` · ${fileCount}` : ''}`}>
                          {secondaryText}
                        </small>
                      </span>
                    </Checkbox>
                  );
                })}
              </Checkbox.Group>
            </Form.Item>
          ))}
          {hasMicroscopyTask && (
            <MicroscopyRecordHumanTask
              taskKind={taskKind}
              form={form}
              inputData={candidatePayload}
              disabled={working}
            />
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
