import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Steps,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  CloudUploadOutlined,
  DownloadOutlined,
  EyeOutlined,
  FileTextOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SafetyCertificateOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import {
  getExecutionFileRoots,
  getExecutionWorkflows,
} from '../../api/execution';
import {
  applyWorkflowReleaseV2,
  exportWorkflowReleaseV2,
  getWorkflowReleaseV2,
  preflightStagedWorkflowReleaseV2,
  preflightWorkflowReleaseV2,
  previewWorkflowV1Migration,
  publishWorkflowReleaseV2,
  rollbackWorkflowReleaseV2,
  updateWorkflowReleaseBindingV2,
} from '../../api/executionV2';
import { isWorkflowReleaseV2Document } from '../../utils/executionWorkflow';
import ExecutionChrome from './ExecutionChrome';
import './execution.css';

const { Paragraph, Text } = Typography;

const releaseOf = payload => payload?.release || payload?.workflow_release || payload;
const reportOf = payload => payload?.report || payload?.preflight || payload;
const releaseIdOf = value => value?.id || value?.release_id;
const workflowIdOf = value => value?.workflow_id || value?.workflow?.id;
const localVersionOf = value => (
  value?.local_version
  ?? value?.workflow_version
  ?? value?.published_version?.version
  ?? value?.activation?.to_version
  ?? (Array.isArray(value?.local_versions) ? value.local_versions.at(-1) : undefined)
);
const activeLocalVersionOf = value => (
  value?.active_local_version
  ?? value?.activation?.to_version
  ?? localVersionOf(value)
);
const tokenOf = report => report?.preflight_token || report?.token;

const issueTone = level => ({
  error: 'error',
  warning: 'warning',
  info: 'info',
}[level] || 'info');

const bindingRowsOf = (report, release) => {
  const rows = report?.required_bindings
    || release?.required_bindings
    || release?.document?.resources?.root_slots
    || release?.portable_document?.resources?.root_slots
    || [];
  return rows.map((item) => {
    if (typeof item === 'string') {
      return { slot: item, access: 'read', required: true };
    }
    return {
      ...item,
      slot: item.slot || item.slot_id || item.slot_name || item.name || item.id,
      access: item.access || item.role || 'read',
      required: item.required !== false,
    };
  }).filter(item => item.slot);
};

const downloadJson = (value, filename) => {
  const blob = new Blob([JSON.stringify(value, null, 2)], {
    type: 'application/json;charset=utf-8',
  });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
};

function PreflightReport({ report, title }) {
  if (!report) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未执行预检" />;
  }
  const issues = Array.isArray(report.issues) ? report.issues : [];
  return (
    <section aria-label={title} className="execution-release-report">
      <div className="execution-release-report__summary">
        <Space wrap>
          <Tag color={report.content_valid ? 'success' : 'error'}>
            内容{report.content_valid ? '有效' : '无效'}
          </Tag>
          <Tag color={report.publish_ready ? 'success' : 'warning'}>
            {report.publish_ready ? '可发布' : '暂不可发布'}
          </Tag>
          {report.release_digest && (
            <Text code copyable>{report.release_digest}</Text>
          )}
        </Space>
        {report.registry_revision && (
          <Text type="secondary">Registry：{report.registry_revision}</Text>
        )}
      </div>
      {issues.length === 0 ? (
        <Alert showIcon type="success" message="预检未发现问题" />
      ) : (
        <List
          size="small"
          dataSource={issues}
          renderItem={(issue, index) => (
            <List.Item>
              <Alert
                showIcon
                type={issueTone(issue.level)}
                message={issue.message || issue.code || `问题 ${index + 1}`}
                description={(
                  <Space size={8} wrap>
                    {issue.code && <Text code>{issue.code}</Text>}
                    {issue.path && <Text type="secondary">{issue.path}</Text>}
                  </Space>
                )}
              />
            </List.Item>
          )}
        />
      )}
    </section>
  );
}

export default function ExecutionWorkflowReleaseManager() {
  const { releaseId: routeReleaseId } = useParams();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const uploadRef = useRef(null);
  const [bindingForm] = Form.useForm();
  const [rollbackForm] = Form.useForm();
  const [migrationForm] = Form.useForm();
  const importedDocument = location.state?.releaseDocument;
  const [documentText, setDocumentText] = useState(
    importedDocument ? JSON.stringify(importedDocument, null, 2) : '',
  );
  const [contentReport, setContentReport] = useState(null);
  const [publishReport, setPublishReport] = useState(null);
  const [release, setRelease] = useState(null);
  const [roots, setRoots] = useState([]);
  const [loading, setLoading] = useState(Boolean(routeReleaseId));
  const [busy, setBusy] = useState('');
  const [loadError, setLoadError] = useState(null);
  const [migrationOpen, setMigrationOpen] = useState(false);
  const [migrationWorkflows, setMigrationWorkflows] = useState([]);
  const [migrationPreview, setMigrationPreview] = useState(null);
  const [migrationLoading, setMigrationLoading] = useState(false);
  const currentReleaseId = routeReleaseId || releaseIdOf(release);
  const currentWorkflowId = workflowIdOf(release) || searchParams.get('workflow_id');

  const loadRelease = useCallback(async () => {
    if (!routeReleaseId) {
      return;
    }
    setLoading(true);
    setLoadError(null);
    try {
      const payload = await getWorkflowReleaseV2(routeReleaseId);
      const next = releaseOf(payload);
      const portableDocument = payload?.document
        || payload?.portable_document
        || next?.document
        || next?.portable_document;
      setRelease(next);
      if (portableDocument) {
        setDocumentText(JSON.stringify(portableDocument, null, 2));
      }
      setContentReport(payload?.content_preflight || next?.content_preflight || null);
      setPublishReport(payload?.publish_preflight || next?.publish_preflight || null);
      const binding = next?.deployment_binding || payload?.deployment_binding;
      const values = {};
      if (Array.isArray(binding?.root_bindings)) {
        binding.root_bindings.forEach((item) => {
          values[item.slot || item.slot_id || item.slot_name] = item.root_id
            || item.storage_root_id;
        });
      } else {
        Object.entries(binding?.bindings?.root_slots || {}).forEach(([slot, item]) => {
          values[slot] = item.root_id || item.storage_root_id;
        });
      }
      bindingForm.setFieldsValue({ roots: values });
    } catch (requestError) {
      setLoadError(requestError);
    } finally {
      setLoading(false);
    }
  }, [bindingForm, routeReleaseId]);

  useEffect(() => {
    loadRelease();
  }, [loadRelease]);

  useEffect(() => {
    getExecutionFileRoots()
      .then(setRoots)
      .catch(() => setRoots([]));
  }, []);

  const requiredBindings = useMemo(
    () => bindingRowsOf(publishReport || contentReport, release),
    [contentReport, publishReport, release],
  );

  const rootOptions = useMemo(() => roots.map(root => ({
    value: root.id || root.root_id,
    label: root.name || root.display_name || root.id || root.root_id,
  })), [roots]);

  const parseDocument = () => {
    let document;
    try {
      document = JSON.parse(documentText);
    } catch (error) {
      throw new Error(`JSON 格式错误：${error.message}`);
    }
    if (!isWorkflowReleaseV2Document(document)) {
      throw new Error('请选择 format=textile-workflow-release、format_version=2.0 的 Release 文件');
    }
    return document;
  };

  const runContentPreflight = async () => {
    setBusy('content-preflight');
    try {
      const payload = await preflightWorkflowReleaseV2(parseDocument());
      const report = reportOf(payload);
      setContentReport(report);
      setPublishReport(null);
      message.success(report.content_valid ? '内容预检通过' : '内容预检已完成');
    } catch (requestError) {
      message.error(requestError.message || '内容预检失败');
    } finally {
      setBusy('');
    }
  };

  const applyRelease = async () => {
    const token = tokenOf(contentReport);
    if (!token) {
      message.error('预检凭据缺失，请重新执行内容预检');
      return;
    }
    setBusy('apply');
    try {
      const payload = await applyWorkflowReleaseV2(token);
      const next = releaseOf(payload);
      setRelease(next);
      const nextId = releaseIdOf(next);
      message.success('Release 已进入 staged 状态');
      if (nextId) {
        navigate(`/execution/admin/releases/${nextId}`, { replace: true });
      }
    } catch (requestError) {
      message.error(requestError.message || '应用 Release 失败');
    } finally {
      setBusy('');
    }
  };

  const saveBinding = async () => {
    if (!currentReleaseId) {
      return;
    }
    setBusy('binding');
    try {
      const values = await bindingForm.validateFields();
      const current = release?.deployment_binding || {};
      const payload = await updateWorkflowReleaseBindingV2(currentReleaseId, {
        environment: current.environment || 'default',
        expected_revision: current.revision ?? release?.binding_revision ?? 0,
        root_bindings: requiredBindings.map(item => ({
          slot: item.slot,
          storage_root_id: values.roots?.[item.slot],
          role: item.access,
        })),
      });
      setRelease(existing => ({
        ...existing,
        deployment_binding: payload?.deployment_binding || payload?.binding || payload,
      }));
      setPublishReport(null);
      message.success('部署绑定已保存；发布前需要重新预检');
    } catch (requestError) {
      if (!requestError?.errorFields) {
        message.error(requestError.message || '保存部署绑定失败');
      }
    } finally {
      setBusy('');
    }
  };

  const runPublishPreflight = async () => {
    setBusy('publish-preflight');
    try {
      const payload = await preflightStagedWorkflowReleaseV2(currentReleaseId);
      const report = reportOf(payload);
      setPublishReport(report);
      message.success(report.publish_ready ? '发布预检通过' : '发布预检已完成');
    } catch (requestError) {
      message.error(requestError.message || '发布预检失败');
    } finally {
      setBusy('');
    }
  };

  const publishRelease = async () => {
    const token = tokenOf(publishReport);
    if (!token) {
      message.error('发布预检凭据缺失，请重新预检');
      return;
    }
    setBusy('publish');
    try {
      const payload = await publishWorkflowReleaseV2(currentReleaseId, token);
      setRelease(existing => ({ ...existing, ...releaseOf(payload) }));
      message.success('Release 已发布，active pointer 已更新');
    } catch (requestError) {
      message.error(requestError.message || '发布 Release 失败');
    } finally {
      setBusy('');
    }
  };

  const exportRelease = async () => {
    const workflowId = currentWorkflowId;
    const localVersion = localVersionOf(release);
    if (!workflowId || !localVersion) {
      message.warning('当前 Release 尚无可导出的本地发布版本');
      return;
    }
    setBusy('export');
    try {
      const payload = await exportWorkflowReleaseV2(workflowId, localVersion);
      const document = payload?.document || payload?.release || payload;
      downloadJson(document, `${document?.release?.slug || 'workflow-release'}-v${document?.release?.release_version || localVersion}.json`);
    } catch (requestError) {
      message.error(requestError.message || '导出失败');
    } finally {
      setBusy('');
    }
  };

  const rollbackRelease = async () => {
    setBusy('rollback');
    try {
      const values = await rollbackForm.validateFields();
      await rollbackWorkflowReleaseV2(currentWorkflowId, {
        target_local_version: values.target_local_version,
        reason: values.reason,
      });
      message.success('active pointer 已回滚；已启动运行不受影响');
      rollbackForm.resetFields();
      await loadRelease();
    } catch (requestError) {
      if (!requestError?.errorFields) {
        message.error(requestError.message || '回滚失败');
      }
    } finally {
      setBusy('');
    }
  };

  const openMigrationPreview = async () => {
    setMigrationOpen(true);
    setMigrationPreview(null);
    try {
      const rows = await getExecutionWorkflows({ include_drafts: true });
      setMigrationWorkflows(rows.filter(item => item.management_mode !== 'release_v2'));
      const initialWorkflowId = searchParams.get('workflow_id');
      if (initialWorkflowId) {
        migrationForm.setFieldValue('workflow_id', initialWorkflowId);
      }
    } catch (requestError) {
      message.error(requestError.message || 'v1 流程列表加载失败');
    }
  };

  const previewMigration = async () => {
    setMigrationLoading(true);
    try {
      const {
        workflow_id: workflowId,
        source,
      } = await migrationForm.validateFields();
      const payload = await previewWorkflowV1Migration(workflowId, source);
      setMigrationPreview(payload);
      message.success('候选与差异已生成；当前流程未发生改变');
    } catch (requestError) {
      if (!requestError?.errorFields) {
        message.error(requestError.message || '生成迁移候选失败');
      }
    } finally {
      setMigrationLoading(false);
    }
  };

  const handleFile = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) {
      return;
    }
    try {
      const text = await file.text();
      const value = JSON.parse(text);
      if (!isWorkflowReleaseV2Document(value)) {
        throw new Error('不是 Workflow Release v2 文件');
      }
      setDocumentText(JSON.stringify(value, null, 2));
      setContentReport(null);
      setPublishReport(null);
      message.success(`已读取 ${file.name}`);
    } catch (error) {
      message.error(error.message || '文件读取失败');
    }
  };

  if (loading) {
    return (
      <div className="execution-route-loading">
        <Spin size="large" />
        <span>正在读取 Workflow Release…</span>
      </div>
    );
  }

  const activeStep = release
    ? (localVersionOf(release) ? 3 : (publishReport ? 2 : 1))
    : (contentReport ? 0 : 0);

  return (
    <div className="execution-page execution-release-page">
      <ExecutionChrome
        title="Workflow Release v2"
        subtitle="以不可变 JSON 契约预检、绑定、发布和回滚工作流"
        backTo={{ path: '/execution/admin', label: 'v1 流程管理' }}
        actions={(
          <Space>
            <Button icon={<EyeOutlined />} onClick={openMigrationPreview}>v1 候选预览</Button>
            {routeReleaseId && (
              <Button icon={<ReloadOutlined />} onClick={loadRelease}>刷新</Button>
            )}
          </Space>
        )}
      />
      <div className="execution-release-body">
        <Alert
          showIcon
          type="info"
          message="Release v2 与旧画布严格隔离"
          description="此页面只处理 format_version=2.0 的 portable Release。预检不会创建流程；apply 只创建 staged Release；只有发布会移动 active pointer。"
        />
        {loadError && (
          <Alert
            showIcon
            type="error"
            message="Release 加载失败"
            description={loadError.message}
            action={<Button onClick={loadRelease}>重试</Button>}
          />
        )}
        <Steps
          current={activeStep}
          items={[
            { title: '内容预检' },
            { title: '部署绑定' },
            { title: '发布预检' },
            { title: '发布/回滚' },
          ]}
        />

        <Row gutter={[20, 20]}>
          <Col xs={24} xl={release ? 10 : 12}>
            <Card
              title={<Space><FileTextOutlined />Release JSON</Space>}
              extra={(
                <Space>
                  <input
                    ref={uploadRef}
                    type="file"
                    accept="application/json,.json"
                    hidden
                    onChange={handleFile}
                  />
                  <Button icon={<UploadOutlined />} onClick={() => uploadRef.current?.click()}>
                    上传 JSON
                  </Button>
                  <Button
                    type="primary"
                    icon={<SafetyCertificateOutlined />}
                    loading={busy === 'content-preflight'}
                    disabled={!documentText.trim() || Boolean(release)}
                    onClick={runContentPreflight}
                  >
                    内容预检
                  </Button>
                </Space>
              )}
            >
              <Input.TextArea
                aria-label="Workflow Release JSON"
                value={documentText}
                onChange={(event) => {
                  setDocumentText(event.target.value);
                  setContentReport(null);
                }}
                readOnly={Boolean(release)}
                rows={20}
                className="execution-json-editor"
                placeholder="上传或粘贴 Workflow Release v2 JSON"
                spellCheck={false}
              />
            </Card>
          </Col>
          <Col xs={24} xl={release ? 14 : 12}>
            <Card
              title="内容预检报告"
              extra={contentReport?.content_valid && !release && (
                <Button
                  type="primary"
                  icon={<CloudUploadOutlined />}
                  loading={busy === 'apply'}
                  disabled={!tokenOf(contentReport)}
                  onClick={applyRelease}
                >
                  Apply 为 staged
                </Button>
              )}
            >
              <PreflightReport report={contentReport} title="内容预检报告" />
            </Card>
          </Col>
        </Row>

        {release && (
          <>
            <Card title="Staged Release">
              <Descriptions size="small" column={{ xs: 1, md: 2, xl: 4 }}>
                <Descriptions.Item label="Release ID">{currentReleaseId}</Descriptions.Item>
                <Descriptions.Item label="状态">
                  <Tag color={localVersionOf(release) ? 'success' : 'processing'}>
                    {release.status || (localVersionOf(release) ? 'published' : 'staged')}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="Workflow">{currentWorkflowId || '待发布创建'}</Descriptions.Item>
                <Descriptions.Item label="最新本地版本">
                  {localVersionOf(release) || '—'}
                </Descriptions.Item>
                <Descriptions.Item label="当前激活版本">
                  {activeLocalVersionOf(release) || '—'}
                </Descriptions.Item>
                <Descriptions.Item label="源标识">
                  {release.source_slug || release.document?.release?.slug || '—'}
                </Descriptions.Item>
                <Descriptions.Item label="源版本">
                  {release.source_version || release.document?.release?.release_version || '—'}
                </Descriptions.Item>
              </Descriptions>
            </Card>

            <Row gutter={[20, 20]}>
              <Col xs={24} xl={10}>
                <Card
                  title="部署绑定"
                  extra={(
                    <Button
                      type="primary"
                      loading={busy === 'binding'}
                      onClick={saveBinding}
                    >
                      保存绑定
                    </Button>
                  )}
                >
                  {requiredBindings.length === 0 ? (
                    <Alert showIcon type="success" message="此 Release 不需要数据根绑定" />
                  ) : (
                    <Form form={bindingForm} layout="vertical">
                      {requiredBindings.map(item => (
                        <Form.Item
                          key={item.slot}
                          name={['roots', item.slot]}
                          label={`${item.slot} · ${item.access}`}
                          rules={item.required ? [{ required: true, message: '请选择数据根' }] : []}
                        >
                          <Select
                            showSearch
                            optionFilterProp="label"
                            placeholder="选择已配置的 StorageRoot"
                            options={rootOptions}
                          />
                        </Form.Item>
                      ))}
                    </Form>
                  )}
                </Card>
              </Col>
              <Col xs={24} xl={14}>
                <Card
                  title="发布预检"
                  extra={(
                    <Space>
                      <Button
                        loading={busy === 'publish-preflight'}
                        onClick={runPublishPreflight}
                      >
                        重新预检
                      </Button>
                      <Button
                        type="primary"
                        icon={<CheckCircleOutlined />}
                        loading={busy === 'publish'}
                        disabled={!publishReport?.publish_ready || !tokenOf(publishReport)}
                        onClick={publishRelease}
                      >
                        发布 Release
                      </Button>
                    </Space>
                  )}
                >
                  <PreflightReport report={publishReport} title="发布预检报告" />
                </Card>
              </Col>
            </Row>

            <Card title="已发布版本操作">
              <Row gutter={[20, 20]} align="bottom">
                <Col xs={24} lg={8}>
                  <Button
                    block
                    icon={<DownloadOutlined />}
                    loading={busy === 'export'}
                    disabled={!currentWorkflowId || !localVersionOf(release)}
                    onClick={exportRelease}
                  >
                    导出 portable Release
                  </Button>
                </Col>
                <Col xs={24} lg={16}>
                  <Form form={rollbackForm} layout="inline" onFinish={rollbackRelease}>
                    <Form.Item
                      name="target_local_version"
                      label="目标本地版本"
                      rules={[{ required: true, message: '请输入目标版本' }]}
                    >
                      <InputNumber min={1} precision={0} />
                    </Form.Item>
                    <Form.Item
                      name="reason"
                      label="回滚原因"
                      rules={[{ required: true, message: '请输入回滚原因' }]}
                    >
                      <Input placeholder="审计必填" />
                    </Form.Item>
                    <Form.Item>
                      <Button
                        htmlType="submit"
                        danger
                        icon={<RollbackOutlined />}
                        loading={busy === 'rollback'}
                        disabled={!currentWorkflowId || !localVersionOf(release)}
                      >
                        回滚 active pointer
                      </Button>
                    </Form.Item>
                  </Form>
                </Col>
              </Row>
            </Card>
          </>
        )}
      </div>

      <Modal
        title="v1 → v2 候选迁移预览"
        open={migrationOpen}
        width={920}
        okText="生成候选与差异"
        cancelText="关闭"
        confirmLoading={migrationLoading}
        onOk={previewMigration}
        onCancel={() => setMigrationOpen(false)}
      >
        <Alert
          showIcon
          type="info"
          message="只读预览"
          description="此操作不会 apply、发布或改变任何流程的 active pointer。"
          style={{ marginBottom: 16 }}
        />
        <Form form={migrationForm} layout="vertical" initialValues={{ source: 'published' }}>
          <Form.Item
            name="workflow_id"
            label="v1 草稿流程"
            rules={[{ required: true, message: '请选择流程' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              options={migrationWorkflows.map(item => ({
                value: item.id || item.workflow_id,
                label: item.name || item.title || item.slug,
              }))}
            />
          </Form.Item>
          <Form.Item name="source" label="候选来源" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'published', label: '当前已发布版本（推荐）' },
                { value: 'draft', label: '当前 v1 草稿' },
              ]}
            />
          </Form.Item>
        </Form>
        {migrationPreview && (
          <>
            <Divider>确定性 candidate / diff</Divider>
            <Paragraph type="secondary">
              预览结果仅用于评审，不提供本轮激活入口。
            </Paragraph>
            <pre className="execution-release-preview">
              {JSON.stringify(migrationPreview, null, 2)}
            </pre>
          </>
        )}
      </Modal>
    </div>
  );
}
