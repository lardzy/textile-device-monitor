import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Divider,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  ApartmentOutlined,
  ArrowRightOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  EditOutlined,
  FileSearchOutlined,
  LockOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  createExecutionRun,
  getExecutionCategories,
  getExecutionRuns,
  getExecutionWorkflows,
} from '../../api/execution';
import {
  clearExecutionRunRequest,
  prepareExecutionRunRequest,
} from '../../utils/executionRunRequest';
import { useExecutionAuth } from './ExecutionAuthContext';
import ExecutionChrome from './ExecutionChrome';
import SchemaFields from './SchemaFields';
import './execution.css';

const { Paragraph, Text, Title } = Typography;

const getCategory = (workflow) => {
  if (workflow.category && typeof workflow.category === 'object') {
    return workflow.category;
  }
  return {
    id: workflow.category_id || workflow.category || 'uncategorized',
    name: workflow.category_name || workflow.category || '未分类',
  };
};

const workflowIdOf = workflow => workflow.id || workflow.workflow_id || workflow.slug;

const publishedVersionOf = workflow => workflow.published_version?.version
  || workflow.published_version
  || workflow.version;

const availabilityOf = (workflow) => {
  if (!publishedVersionOf(workflow)) {
    return { available: false, message: '流程尚未发布' };
  }
  const available = workflow.runnable !== false
    && workflow.available !== false
    && workflow.availability?.available !== false
    && workflow.root_status !== 'missing'
    && workflow.data_root_status !== 'missing'
    && workflow.is_enabled !== false;
  return {
    available,
    message: workflow.unavailable_reason
      || workflow.availability?.message
      || (workflow.is_enabled === false ? '流程已停用' : '所需数据根或运行能力尚未配置'),
  };
};

const withInspectionNumber = (schema = {}) => ({
  type: 'object',
  ...schema,
  properties: {
    inspection_number: {
      type: 'string',
      title: '检验编号',
      description: '默认带入流程目录顶部输入的编号，可在开始前核对修改。',
    },
    ...(schema.properties || {}),
  },
  required: [...new Set(['inspection_number', ...(schema.required || [])])],
});

const schemaDefaults = schema => Object.fromEntries(
  Object.entries(schema?.properties || {})
    .filter(([, field]) => field.default !== undefined)
    .map(([name, field]) => [name, field.default]),
);

const runStatusMeta = {
  running: { color: 'processing', text: '最近运行中' },
  waiting_human: { color: 'warning', text: '等待人工处理' },
  completed: { color: 'success', text: '最近已完成' },
  succeeded: { color: 'success', text: '最近已完成' },
  failed: { color: 'error', text: '最近失败' },
  cancel_pending: { color: 'warning', text: '正在安全取消' },
  failure_pending: { color: 'error', text: '失败收尾中' },
  cancelled: { color: 'default', text: '最近已取消' },
};

function WorkflowCard({
  workflow,
  canRun,
  onRun,
  onDesign,
  creating,
}) {
  const category = getCategory(workflow);
  const workflowId = workflowIdOf(workflow);
  const availability = availabilityOf(workflow);
  const { available } = availability;
  const actionable = available && canRun;
  const version = publishedVersionOf(workflow);
  const requiredCount = workflow.required_input_count
    ?? workflow.input_schema?.required?.length
    ?? workflow.published_definition?.input_schema?.required?.length
    ?? 0;
  const lastRun = workflow.last_run || workflow.latest_run;
  const lastStatus = runStatusMeta[lastRun?.status];
  const capabilities = workflow.capabilities || {};
  const canWrite = (Array.isArray(capabilities)
    ? capabilities.includes('write')
    : capabilities.write === true)
    || workflow.write_enabled
    || workflow.access_mode === 'write';

  return (
    <Card
      className={`execution-workflow-card ${available ? '' : 'is-unavailable'}`}
      hoverable={actionable}
      onClick={() => actionable && onRun(workflow)}
      actions={[
        <Tooltip
          key="run"
          title={!canRun && available ? '当前账号没有运行流程权限' : undefined}
        >
          <Button
            type="link"
            disabled={!actionable}
            loading={creating === workflowId}
            onClick={(event) => {
              event.stopPropagation();
              if (actionable) {
                onRun(workflow);
              }
            }}
          >
            {available ? '开始执行' : '暂不可运行'} <ArrowRightOutlined />
          </Button>
        </Tooltip>,
        onDesign ? (
          <Button
            key="design"
            type="text"
            icon={<EditOutlined />}
            onClick={(event) => {
              event.stopPropagation();
              onDesign(workflow);
            }}
          >
            设计
          </Button>
        ) : (
          <span key="version">v{version || '—'}</span>
        ),
      ]}
    >
      <div className="execution-workflow-card__top">
        <span className="execution-workflow-card__icon">
          {available ? <ApartmentOutlined /> : <StopOutlined />}
        </span>
        <Space size={6} wrap>
          <Tag color="geekblue">{category.name}</Tag>
          <Tag icon={canWrite ? <EditOutlined /> : <LockOutlined />}>
            {canWrite ? '受控写入' : '只读'}
          </Tag>
        </Space>
      </div>
      <Title level={4}>{workflow.name || workflow.title}</Title>
      <Paragraph className="execution-workflow-card__description">
        {workflow.description || '按已发布流程处理检测资料并保留完整执行记录。'}
      </Paragraph>
      <div className="execution-workflow-card__meta">
        <span><CheckCircleFilled /> 已发布 v{version || '—'}</span>
        <span><FileSearchOutlined /> {requiredCount} 项必填</span>
      </div>
      <div className="execution-workflow-card__footer">
        {!available ? (
          <Tooltip title={availability.message}>
            <Text type="danger">
              <StopOutlined /> {availability.message}
            </Text>
          </Tooltip>
        ) : lastStatus ? (
          <Tag color={lastStatus.color}>{lastStatus.text}</Tag>
        ) : (
          <Text type="secondary">尚无运行记录</Text>
        )}
        <Text type="secondary">
          <ClockCircleOutlined /> {workflow.updated_at
            ? dayjs(workflow.updated_at).format('MM-DD HH:mm')
            : '暂无更新'}
        </Text>
      </div>
    </Card>
  );
}

export default function ExecutionCatalog() {
  const [runForm] = Form.useForm();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const {
    canDesignWorkflow,
    canRunWorkflow,
  } = useExecutionAuth();
  const [inspectionNumber, setInspectionNumber] = useState(
    searchParams.get('number') || '',
  );
  const [selectedCategories, setSelectedCategories] = useState(
    searchParams.getAll('category'),
  );
  const [categories, setCategories] = useState([]);
  const [workflows, setWorkflows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [creating, setCreating] = useState(null);
  const [runWorkflow, setRunWorkflow] = useState(null);
  const [runFormDefaults, setRunFormDefaults] = useState(null);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [categoryRows, workflowRows, runRows] = await Promise.all([
        getExecutionCategories(),
        getExecutionWorkflows({ published: true }),
        canRunWorkflow ? getExecutionRuns({ limit: 200 }) : Promise.resolve([]),
      ]);
      const latestRunByWorkflow = new Map();
      runRows.forEach((run) => {
        if (!latestRunByWorkflow.has(run.workflow_id)) {
          latestRunByWorkflow.set(run.workflow_id, run);
        }
      });
      setCategories(categoryRows);
      setWorkflows(workflowRows.map(workflow => ({
        ...workflow,
        last_run: workflow.last_run || latestRunByWorkflow.get(workflowIdOf(workflow)),
      })));
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [canRunWorkflow]);

  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    const next = new URLSearchParams();
    if (inspectionNumber.trim()) {
      next.set('number', inspectionNumber.trim());
    }
    selectedCategories.forEach(category => next.append('category', category));
    setSearchParams(next, { replace: true });
  }, [inspectionNumber, selectedCategories, setSearchParams]);

  const categoryOptions = useMemo(() => {
    const fromApi = categories.map(category => ({
      value: String(category.id || category.slug || category.name),
      label: category.name,
    }));
    if (fromApi.length) {
      return fromApi;
    }
    return [...new Map(workflows.map((workflow) => {
      const category = getCategory(workflow);
      return [String(category.id), { value: String(category.id), label: category.name }];
    })).values()];
  }, [categories, workflows]);

  const groupedWorkflows = useMemo(() => {
    const groups = new Map();
    workflows.forEach((workflow) => {
      const category = getCategory(workflow);
      const id = String(category.id);
      if (selectedCategories.length && !selectedCategories.includes(id)) {
        return;
      }
      if (!groups.has(id)) {
        groups.set(id, { category, workflows: [] });
      }
      groups.get(id).workflows.push(workflow);
    });
    return [...groups.values()];
  }, [selectedCategories, workflows]);

  const openRunForm = (workflow) => {
    if (!availabilityOf(workflow).available || !canRunWorkflow) {
      return;
    }
    if (!inspectionNumber.trim()) {
      message.warning('请先输入检验编号');
      return;
    }
    const inputSchema = withInspectionNumber(workflow.input_schema || {});
    const globalSchema = workflow.global_schema || {
      type: 'object',
      properties: {},
    };
    setRunFormDefaults({
      input_data: {
        ...schemaDefaults(inputSchema),
        inspection_number: inspectionNumber.trim(),
      },
      global_data: schemaDefaults(globalSchema),
    });
    setRunWorkflow({
      ...workflow,
      input_schema: inputSchema,
      global_schema: globalSchema,
    });
  };

  useEffect(() => {
    if (!runWorkflow || !runFormDefaults) {
      return;
    }
    runForm.resetFields();
    runForm.setFieldsValue(runFormDefaults);
  }, [runForm, runFormDefaults, runWorkflow]);

  const startRun = async (values) => {
    if (!runWorkflow) {
      return;
    }
    const normalizedNumber = values.input_data?.inspection_number?.trim();
    if (!normalizedNumber) {
      message.warning('请填写检验编号');
      return;
    }
    const inputData = {
      ...(values.input_data || {}),
      inspection_number: normalizedNumber,
    };
    const request = prepareExecutionRunRequest({
      workflow_id: workflowIdOf(runWorkflow),
      inspection_number: normalizedNumber,
      input_data: inputData,
      global_data: values.global_data || {},
    });
    const workflowId = workflowIdOf(runWorkflow);
    setCreating(workflowId);
    try {
      const result = await createExecutionRun(request.payload);
      const run = result?.run || result;
      const runId = run?.id || run?.run_id;
      if (!runId) {
        throw new Error('服务器未返回运行编号');
      }
      clearExecutionRunRequest(request.signature);
      setRunWorkflow(null);
      navigate(`/execution/runs/${runId}`);
    } catch (requestError) {
      message.error(requestError.message || '创建执行任务失败');
    } finally {
      setCreating(null);
    }
  };

  return (
    <div className="execution-page execution-catalog">
      <ExecutionChrome
        title="执行系统"
        subtitle="选择流程，让重复的检测工作按标准步骤可靠执行"
        actions={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={loadCatalog}>刷新</Button>
            {canDesignWorkflow && (
              <Button
                type="primary"
                icon={<EditOutlined />}
                onClick={() => navigate('/execution/admin')}
              >
                流程管理
              </Button>
            )}
          </Space>
        )}
      />

      <section className="execution-catalog__search">
        <div className="execution-catalog__search-copy">
          <Text className="execution-eyebrow">开始一项检测工作</Text>
          <Title level={3}>输入编号，选择适用流程</Title>
          <Paragraph>
            编号会自动带入执行界面；类别可多选，帮助您快速缩小范围。
          </Paragraph>
        </div>
        <div className="execution-catalog__filters">
          <Input
            size="large"
            value={inspectionNumber}
            onChange={event => setInspectionNumber(event.target.value)}
            onPressEnter={() => document.querySelector('.execution-workflow-card:not(.is-unavailable)')?.click()}
            prefix={<SearchOutlined />}
            placeholder="输入检验编号，例如 26X910095-1"
            allowClear
            aria-label="检验编号"
          />
          <Select
            size="large"
            mode="multiple"
            maxTagCount="responsive"
            value={selectedCategories}
            onChange={setSelectedCategories}
            options={categoryOptions}
            placeholder="全部类别"
            allowClear
            aria-label="类别筛选"
          />
        </div>
      </section>

      {error && (
        <Alert
          showIcon
          type="error"
          message="流程目录加载失败"
          description={error.message}
          action={<Button onClick={loadCatalog}>重试</Button>}
        />
      )}

      {loading ? (
        <div className="execution-catalog__skeleton">
          {[0, 1, 2, 3].map(item => <Card key={item}><Skeleton active /></Card>)}
        </div>
      ) : groupedWorkflows.length ? (
        <div className="execution-workflow-groups">
          {groupedWorkflows.map(group => (
            <section className="execution-workflow-group" key={group.category.id}>
              <div className="execution-workflow-group__header">
                <div>
                  <h2>{group.category.name}</h2>
                  {group.category.description && <p>{group.category.description}</p>}
                </div>
                <Tag>{group.workflows.length} 个流程</Tag>
              </div>
              <div className="execution-workflow-grid">
                {group.workflows.map(workflow => (
                  <WorkflowCard
                    key={workflowIdOf(workflow)}
                    workflow={workflow}
                    canRun={canRunWorkflow}
                    onRun={openRunForm}
                    onDesign={canDesignWorkflow
                      ? item => navigate(`/execution/admin/workflows/${workflowIdOf(item)}`)
                      : null}
                    creating={creating}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="没有符合当前筛选条件的已发布流程"
        >
          <Button onClick={() => setSelectedCategories([])}>清除类别筛选</Button>
        </Empty>
      )}

      <Modal
        title={`开始执行：${runWorkflow?.name || ''}`}
        open={Boolean(runWorkflow)}
        okText="确认并开始"
        cancelText="取消"
        confirmLoading={Boolean(creating)}
        onOk={() => runForm.submit()}
        onCancel={() => {
          if (!creating) {
            setRunWorkflow(null);
          }
        }}
        destroyOnHidden
        forceRender
        width={620}
      >
        <Alert
          showIcon
          type="info"
          message="请在创建运行前核对完整输入"
          description="必填项和选填项会随本次运行一起保存；进入工作台后仅可查看，不能修改。"
          style={{ marginBottom: 18 }}
        />
        <Form
          form={runForm}
          layout="vertical"
          onFinish={startRun}
          initialValues={runFormDefaults || undefined}
        >
          <SchemaFields schema={runWorkflow?.input_schema} namePrefix="input_data" />
          {Object.keys(runWorkflow?.global_schema?.properties || {}).length > 0 && (
            <>
              <Divider orientation="left">流程全局变量</Divider>
              <SchemaFields schema={runWorkflow.global_schema} namePrefix="global_data" />
            </>
          )}
        </Form>
      </Modal>
    </div>
  );
}
