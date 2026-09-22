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
  DownloadOutlined,
  FileTextOutlined,
  ReloadOutlined,
  RollbackOutlined,
  SafetyCertificateOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import {
  getExecutionFileRoots,
  getExecutionCredentials,
  getProjectRules,
} from '../../api/execution';
import {
  applyWorkflowReleaseV2,
  exportWorkflowReleaseV2,
  getWorkflowReleaseV2,
  getWorkflowReleasesV2,
  getExecutionV2Monitoring,
  getExecutionV2Packs,
  getExecutionV2RendererCapabilities,
  preflightStagedWorkflowReleaseV2,
  preflightWorkflowReleaseV2,
  publishWorkflowReleaseV2,
  rollbackWorkflowReleaseV2,
  updateWorkflowReleaseBindingV2,
} from '../../api/executionV2';
import { isWorkflowReleaseV2Document } from '../../utils/executionWorkflow';
import ExecutionChrome from './ExecutionChrome';
import WorkflowReplacementPanel from './WorkflowReplacementPanel';
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
  return rows.filter(item => !item?.kind || item.kind === 'root_slot').map((item) => {
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
  const importedDocument = location.state?.releaseDocument;
  const [documentText, setDocumentText] = useState(
    importedDocument ? JSON.stringify(importedDocument, null, 2) : '',
  );
  const [contentReport, setContentReport] = useState(null);
  const [publishReport, setPublishReport] = useState(null);
  const [release, setRelease] = useState(null);
  const [roots, setRoots] = useState([]);
  const [projectRules, setProjectRules] = useState([]);
  const [credentials, setCredentials] = useState([]);
  const [loading, setLoading] = useState(Boolean(routeReleaseId));
  const [busy, setBusy] = useState('');
  const [loadError, setLoadError] = useState(null);
  const [releaseList, setReleaseList] = useState([]);
  const [registryHealth, setRegistryHealth] = useState(null);
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
      bindingForm.setFieldsValue({
        roots: values,
        credentials: Object.fromEntries(Object.entries(binding?.bindings?.credential_slots || {})
          .map(([slot, item]) => [slot, item.credential_id])),
        rules: Object.fromEntries(Object.entries(binding?.bindings?.rule_slots || {})
          .map(([slot, item]) => [slot, item.rule_key])),
      });
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

  const loadRegistryDashboard = useCallback(async () => {
    const [releaseResult, packResult, rendererResult, monitoringResult] = await Promise.allSettled([
      getWorkflowReleasesV2({ limit: 30 }),
      getExecutionV2Packs(),
      getExecutionV2RendererCapabilities(),
      getExecutionV2Monitoring(24),
    ]);
    if (releaseResult.status === 'fulfilled') {
      setReleaseList(releaseResult.value?.items || []);
    }
    const packs = packResult.status === 'fulfilled' ? packResult.value : [];
    const rendererPayload = rendererResult.status === 'fulfilled'
      ? rendererResult.value
      : null;
    const monitoringPayload = monitoringResult.status === 'fulfilled'
      ? monitoringResult.value
      : null;
    setRegistryHealth({
      packs,
      renderers: rendererPayload?.items || [],
      registryRevision: rendererPayload?.registry_revision
        || monitoringPayload?.registry_revision,
      rolloutProfile: rendererPayload?.rollout_profile
        || monitoringPayload?.rollout_profile,
      monitoring: monitoringPayload,
    });
  }, []);

  useEffect(() => {
    loadRegistryDashboard();
  }, [loadRegistryDashboard]);

  const requiredBindings = useMemo(
    () => bindingRowsOf(publishReport || contentReport, release),
    [contentReport, publishReport, release],
  );

  const rootOptions = useMemo(() => roots.map(root => ({
    value: root.id || root.root_id,
    label: root.name || root.display_name || root.id || root.root_id,
  })), [roots]);

  const ruleSlots = useMemo(() => {
    try {
      return (JSON.parse(documentText).resources?.rule_slots || []).filter(slot => !slot.definition);
    } catch {
      return [];
    }
  }, [documentText]);

  useEffect(() => {
    if (!ruleSlots.length) return undefined;
    let active = true;
    getProjectRules().then((items) => {
      if (!active) return;
      setProjectRules(items);
      for (const slot of ruleSlots) {
        const matches = items.filter(rule => rule.enabled && rule.display_name === slot.name);
        if (!bindingForm.getFieldValue(['rules', slot.slot_id]) && matches.length === 1) {
          bindingForm.setFieldValue(['rules', slot.slot_id], matches[0].rule_key);
        }
      }
    }).catch(error => { if (active) message.error(error.message || '读取匹配规则失败'); });
    return () => { active = false; };
  }, [ruleSlots, bindingForm]);

  const credentialSlots = useMemo(() => {
    try { return JSON.parse(documentText).resources?.credential_slots || []; }
    catch { return []; }
  }, [documentText]);

  useEffect(() => {
    if (!credentialSlots.length) return undefined;
    let active = true;
    getExecutionCredentials().then(items => {
      if (!active) return;
      setCredentials(items.filter(item => item.is_active && item.configured));
      for (const slot of credentialSlots) {
        const key = slot.connector_id === 'legacy_fibrecheck' ? 'legacy_inspection' : slot.connector_id;
        const matches = items.filter(item => item.is_active && item.configured && item.system_key === key);
        if (!bindingForm.getFieldValue(['credentials', slot.slot_id]) && matches.length === 1) {
          bindingForm.setFieldValue(['credentials', slot.slot_id], matches[0].id);
        }
      }
    }).catch(error => { if (active) message.error(error.message || '读取检务账号失败'); });
    return () => { active = false; };
  }, [credentialSlots, bindingForm]);

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

  const applyRelease = async () => {
    if (busy) return;
    setBusy('apply');
    try {
      const report = reportOf(await preflightWorkflowReleaseV2(parseDocument()));
      setContentReport(report);
      setPublishReport(null);
      if (!report.content_valid || !tokenOf(report)) {
        message.error('请先修正下方检查结果');
        return;
      }
      const token = tokenOf(report);
      const payload = await applyWorkflowReleaseV2(token);
      const next = releaseOf(payload);
      setRelease(next);
      const nextId = releaseIdOf(next);
      message.success('已导入，可配置并发布');
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

    const values = await bindingForm.validateFields();
    const current = release?.deployment_binding || {};
    const existing = Array.isArray(current.root_bindings)
      ? Object.fromEntries(current.root_bindings.map(item => [item.slot || item.slot_id || item.slot_name, item.root_id || item.storage_root_id]))
      : Object.fromEntries(Object.entries(current.bindings?.root_slots || {})
        .map(([slot, item]) => [slot, item.root_id || item.storage_root_id]));
    const selectedRules = Object.fromEntries(ruleSlots.map((slot) => {
      const rule = projectRules.find(item => item.rule_key === values.rules?.[slot.slot_id]);
      if (!rule) throw new Error('请选择可用的匹配规则');
      return [slot.slot_id, { rule_key: rule.rule_key, revision: rule.revision }];
    }));
    const selectedCredentials = Object.fromEntries(credentialSlots.map(slot => {
      const credential = credentials.find(item => item.id === values.credentials?.[slot.slot_id]);
      if (!credential) throw new Error('请先配置并选择检务账号');
      return [slot.slot_id, { credential_id: credential.id, revision: credential.revision }];
    }));
    const changed = !release?.deployment_binding || Object.entries(selectedCredentials).some(([slot, item]) => {
      const previous = current.bindings?.credential_slots?.[slot];
      return previous?.credential_id !== item.credential_id || previous?.revision !== item.revision;
    }) || requiredBindings.some(item => existing[item.slot] !== values.roots?.[item.slot])
      || Object.entries(selectedRules).some(([slot, rule]) => {
        const previous = current.bindings?.rule_slots?.[slot];
        return previous?.rule_key !== rule.rule_key || previous?.revision !== rule.revision;
      });
    if (!changed) return;
    const rootBindings = requiredBindings.map(item => ({
      slot: item.slot, storage_root_id: values.roots?.[item.slot], role: item.access,
    }));
    const payload = await updateWorkflowReleaseBindingV2(currentReleaseId, {
        environment: current.environment || 'default',
        expected_revision: current.revision ?? release?.binding_revision ?? 0,
        ...(ruleSlots.length || credentialSlots.length ? {
          bindings: {
            root_slots: Object.fromEntries(rootBindings.map((item) => {
              const root = roots.find(value => value.id === item.storage_root_id || value.root_id === item.storage_root_id);
              if (!root) throw new Error('请选择可用的数据根');
              return [item.slot, { root_id: root.root_id, revision: root.binding_revision || 1 }];
            })),
            rule_slots: selectedRules,
            role_slots: Object.fromEntries(Object.entries(current.bindings?.role_slots || {}).map(([slot, item]) => [slot, { role_key: item.role_key }])),
            credential_slots: selectedCredentials,
          },
        } : { root_bindings: rootBindings }),
    });
    setRelease(previous => ({
      ...previous,
      deployment_binding: payload?.deployment_binding || payload?.binding || payload,
    }));
    setPublishReport(null);
  };

  const runPublishPreflight = async () => {
    if (busy) return;
    setBusy('publish-preflight');
    try {
      await saveBinding();
      const payload = await preflightStagedWorkflowReleaseV2(currentReleaseId,
        release?.migration_source ? { replacement_source: release.migration_source } : {});
      const report = reportOf(payload);
      setPublishReport(report);
      message.success(report.publish_ready ? '发布预检通过' : '发布预检已完成');
    } catch (requestError) {
      if (!requestError?.errorFields) message.error(requestError.message || '发布预检失败');
    } finally {
      setBusy('');
    }
  };

  const publishRelease = async () => {
    if (busy) return;
    setBusy('publish');
    try {
      await saveBinding();
      const report = reportOf(await preflightStagedWorkflowReleaseV2(currentReleaseId,
        release?.migration_source ? { replacement_source: release.migration_source } : {}));
      setPublishReport(report);
      if (!report.publish_ready || !tokenOf(report)) {
        message.error('暂不能发布，请查看检查结果');
        return;
      }
      const token = tokenOf(report);
      const payload = await publishWorkflowReleaseV2(currentReleaseId, token);
      setRelease(existing => ({ ...existing, ...releaseOf(payload) }));
      setPublishReport(null);
      message.success(releaseOf(payload).migration_source ? 'Release 已发布，请核对接替状态' : 'Release 已发布，当前版本已更新');
    } catch (requestError) {
      if (!requestError?.errorFields) message.error(requestError.message || '发布失败');
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

  const activeStep = release ? (localVersionOf(release) ? 2 : 1) : 0;

  return (
    <div className="execution-page execution-release-page">
      <ExecutionChrome
        title="工作流发布"
        subtitle="绑定资源、发布新版本与回退"
        backTo={{ path: '/execution/admin', label: '流程管理' }}
        actions={(
          <Space>
            <Button onClick={() => {
              if (routeReleaseId) navigate(`/execution/admin/designer/${routeReleaseId}`);
              else {
                try { navigate('/execution/admin/designer', { state: { releaseDocument: JSON.parse(documentText) } }); }
                catch { navigate('/execution/admin/designer'); }
              }
            }}>{routeReleaseId ? '设计下一版本' : '打开设计器'}</Button>
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
          message="发布新版本"
          description="确认资源绑定后发布，已运行的任务继续使用原版本。"
        />
        <details>
          <summary>运行环境</summary>
        {registryHealth && (
          <Card title="Registry / Pack / Renderer / Rollout 状态" size="small">
            <Descriptions size="small" column={{ xs: 1, md: 2, xl: 4 }}>
              <Descriptions.Item label="Rollout profile">
                <Tag color="blue">{registryHealth.rolloutProfile || '未知'}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Registry revision">
                <Text code copyable>{registryHealth.registryRevision || '—'}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="Pack readiness">
                {registryHealth.packs.filter(item => item.ready !== false).length}
                /{registryHealth.packs.length}
              </Descriptions.Item>
              <Descriptions.Item label="Renderer readiness">
                {registryHealth.renderers.filter(item => item.ready).length}
                /{registryHealth.renderers.length}
              </Descriptions.Item>
              <Descriptions.Item label="24h shadow mismatch">
                {registryHealth.monitoring?.shadow_mismatch?.count ?? '—'}
              </Descriptions.Item>
              <Descriptions.Item label="能力不可用节点">
                {registryHealth.monitoring?.node_capability_unavailable?.unavailable_node_count ?? '—'}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        )}
        </details>
        {!routeReleaseId && releaseList.length > 0 && (
          <Card title="Workflow Release 列表（最近 30 项）" size="small">
            <List
              dataSource={releaseList}
              renderItem={item => (
                <List.Item
                  actions={[
                    <Button
                      key="open"
                      type="link"
                      onClick={() => navigate(`/execution/admin/releases/${item.id}`)}
                    >
                      打开
                    </Button>,
                  ]}
                >
                  <List.Item.Meta
                    title={<Space>{item.source_slug}<Tag>{item.status}</Tag></Space>}
                    description={(
                      <Space wrap>
                        <Tag color="purple">native {item.native_node_type_count ?? 0}</Tag>
                        <Text type="secondary">{item.release_digest}</Text>
                      </Space>
                    )}
                  />
                </List.Item>
              )}
            />
          </Card>
        )}
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
            { title: '导入' },
            { title: '配置并发布' },
            { title: '版本与接替' },
          ]}
        />

        <details open={!release}>
          <summary>流程 JSON 与检查明细</summary>
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
                    loading={busy === 'apply'}
                    disabled={!documentText.trim() || Boolean(release) || Boolean(busy)}
                    onClick={applyRelease}
                  >
                    导入并检查
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
                  setPublishReport(null);
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
            >
              <PreflightReport report={contentReport} title="内容预检报告" />
            </Card>
          </Col>
        </Row>
        </details>

        {release && (
          <>
            <Card title="版本信息">
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

            {release.migration_source && <Alert type="info" message="已冻结本地来源" description="此版本保留发布时的来源记录。" />}

            <Row gutter={[20, 20]}>
              <Col xs={24} xl={10}>
                <Card
                  title="部署绑定"
                >
                  {requiredBindings.length === 0 && ruleSlots.length === 0 && credentialSlots.length === 0 ? (
                    <Alert showIcon type="success" message="此 Release 不需要数据根绑定" />
                  ) : (
                    <Form form={bindingForm} layout="vertical" disabled={Boolean(busy) || Boolean(localVersionOf(release))}>
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
                      {credentialSlots.map(slot => (
                        <Form.Item key={slot.slot_id} name={['credentials', slot.slot_id]} label={slot.name || '检务账号'}
                          rules={[{ required: slot.required, message: '请选择检务账号' }]}>
                          <Select placeholder="选择已配置的账号" options={credentials.filter(item => item.system_key === (slot.connector_id === 'legacy_fibrecheck' ? 'legacy_inspection' : slot.connector_id))
                            .map(item => ({ value: item.id, label: item.account_name || item.system_key }))} />
                        </Form.Item>
                      ))}
                      {ruleSlots.map(slot => (
                        <Form.Item key={slot.slot_id} name={['rules', slot.slot_id]} label={slot.name || slot.slot_id}
                          rules={[{ required: slot.required, message: '请选择匹配规则' }]}>
                          <Select options={projectRules.filter(rule => rule.enabled).map(rule => ({
                            value: rule.rule_key, label: rule.display_name,
                          }))} />
                        </Form.Item>
                      ))}
                    </Form>
                  )}
                </Card>
              </Col>
              <Col xs={24} xl={14}>
                <Card
                  title="发布检查"
                  extra={(
                    <Space>
                      <Button
                        loading={busy === 'publish-preflight'}
                        disabled={Boolean(busy)}
                        onClick={runPublishPreflight}
                      >
                        仅检查
                      </Button>
                      <Button
                        type="primary"
                        icon={<CheckCircleOutlined />}
                        loading={busy === 'publish'}
                        disabled={Boolean(busy) || Boolean(localVersionOf(release))}
                        onClick={publishRelease}
                      >
                        检查并发布
                      </Button>
                    </Space>
                  )}
                >
                  {localVersionOf(release) ? <Alert type="success" showIcon message="当前版本已发布" /> : <PreflightReport report={publishReport} title="发布预检报告" />}
                </Card>
              </Col>
            </Row>

            {currentWorkflowId && release.migration_source && (
              <WorkflowReplacementPanel workflowId={currentWorkflowId} revisionKey={activeLocalVersionOf(release)} />
            )}

            {Boolean(localVersionOf(release)) && <Card title="已发布版本操作">
              <Row gutter={[20, 20]} align="bottom">
                <Col xs={24} lg={8}>
                  <Button
                    block
                    icon={<DownloadOutlined />}
                    loading={busy === 'export'}
                    disabled={!currentWorkflowId || !localVersionOf(release)}
                    onClick={exportRelease}
                  >
                    导出流程 JSON
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
                        回滚到所选版本
                      </Button>
                    </Form.Item>
                  </Form>
                </Col>
              </Row>
            </Card>}
          </>
        )}
      </div>


    </div>
  );
}
