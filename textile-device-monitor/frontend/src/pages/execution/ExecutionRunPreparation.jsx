import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Form,
  Result,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  ApartmentOutlined,
  CheckCircleOutlined,
  FileSearchOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  createExecutionRun,
  getExecutionWorkflow,
} from '../../api/execution';
import {
  clearExecutionRunRequest,
  prepareExecutionRunRequest,
} from '../../utils/executionRunRequest';
import { normalizeWorkflowDefinition } from '../../utils/executionWorkflow';
import ExecutionChrome from './ExecutionChrome';
import SchemaFields from './SchemaFields';
import WorkflowCanvas from './WorkflowCanvas';
import './execution.css';

const { Paragraph, Text, Title } = Typography;

const withInspectionNumber = (schema = {}) => ({
  type: 'object',
  ...schema,
  properties: {
    inspection_number: {
      type: 'string',
      title: '检验编号',
      description: '可从流程目录带入，也可以在这里补充或修改。',
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

const categoryNameOf = workflow => (
  workflow?.category?.name
  || workflow?.category_name
  || '未分类'
);

const availabilityOf = workflow => (
  workflow?.is_enabled !== false
  && workflow?.availability?.available !== false
  && Boolean(workflow?.published_version)
);

export default function ExecutionRunPreparation() {
  const { workflowId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialNumberRef = useRef(searchParams.get('number') || '');
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const [workflow, setWorkflow] = useState(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState(null);

  const loadWorkflow = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setWorkflow(await getExecutionWorkflow(workflowId));
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    loadWorkflow();
  }, [loadWorkflow]);

  const definition = useMemo(() => normalizeWorkflowDefinition(
    workflow?.published_definition,
    {
      name: workflow?.name,
      category: workflow?.category,
    },
  ), [workflow]);
  const inputSchema = useMemo(
    () => withInspectionNumber(
      workflow?.input_schema
      || definition.input_schema
      || {},
    ),
    [definition.input_schema, workflow?.input_schema],
  );
  const globalSchema = workflow?.global_schema
    || definition.global_schema
    || { type: 'object', properties: {} };
  const canStart = availabilityOf(workflow);

  useEffect(() => {
    if (!workflow) {
      return;
    }
    form.setFieldsValue({
      input_data: {
        ...schemaDefaults(inputSchema),
        inspection_number: initialNumberRef.current,
      },
      global_data: schemaDefaults(globalSchema),
    });
  }, [form, globalSchema, inputSchema, workflow]);

  const keepNumberInUrl = (_, values) => {
    const next = new URLSearchParams(searchParams);
    const number = values.input_data?.inspection_number?.trim();
    if (number) {
      next.set('number', number);
    } else {
      next.delete('number');
    }
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true });
    }
  };

  const startRun = async (values) => {
    const normalizedNumber = values.input_data?.inspection_number?.trim();
    if (!normalizedNumber) {
      message.warning('请填写检验编号后再开始执行');
      return;
    }
    const inputData = {
      ...(values.input_data || {}),
      inspection_number: normalizedNumber,
    };
    const request = prepareExecutionRunRequest({
      workflow_id: workflow.id,
      inspection_number: normalizedNumber,
      input_data: inputData,
      global_data: values.global_data || {},
    });
    setCreating(true);
    try {
      const result = await createExecutionRun(request.payload);
      const run = result?.run || result;
      const runId = run?.id || run?.run_id;
      if (!runId) {
        throw new Error('服务器未返回运行编号');
      }
      clearExecutionRunRequest(request.signature);
      navigate(`/execution/runs/${runId}`, { replace: true });
    } catch (requestError) {
      message.error(requestError.message || '创建执行任务失败');
    } finally {
      setCreating(false);
    }
  };

  if (loading) {
    return (
      <div className="execution-route-loading">
        <Spin size="large" />
        <span>正在读取已发布流程…</span>
      </div>
    );
  }

  if (error || !workflow) {
    return (
      <Result
        status={error?.status === 404 ? '404' : 'warning'}
        title={error?.status === 404 ? '流程不存在或不可访问' : '流程加载失败'}
        subTitle={error?.message}
        extra={(
          <Space>
            <Button onClick={() => navigate('/execution')}>返回流程目录</Button>
            <Button type="primary" onClick={loadWorkflow}>重试</Button>
          </Space>
        )}
      />
    );
  }

  return (
    <div className="execution-preparation">
      <ExecutionChrome
        title={workflow.name || '执行准备'}
        subtitle="先核对流程与输入，确认后才会创建运行记录"
        backTo={{ path: '/execution', label: '流程目录' }}
      />

      {!canStart && (
        <Alert
          banner
          showIcon
          type="warning"
          message="当前流程暂不可运行"
          description={workflow.availability?.message || '流程未发布或已停用。'}
        />
      )}

      <main className="execution-preparation__grid">
        <aside className="execution-preparation__form">
          <div className="execution-workspace__section-title">
            <div>
              <Text className="execution-eyebrow">RUN INPUTS</Text>
              <Title level={4}>本次执行信息</Title>
            </div>
            <Tag color="blue">执行前</Tag>
          </div>
          <Alert
            showIcon
            type="info"
            message="尚未创建运行记录"
            description="可先查看流程图并补充编号；只有点击“开始执行”后才会进入执行记录。"
          />
          <Form
            form={form}
            layout="vertical"
            onFinish={startRun}
            onValuesChange={keepNumberInUrl}
            className="execution-preparation__input-form"
          >
            <Divider orientation="left">必填与选填信息</Divider>
            <SchemaFields schema={inputSchema} namePrefix="input_data" />
            {Object.keys(globalSchema?.properties || {}).length > 0 && (
              <>
                <Divider orientation="left">流程全局变量</Divider>
                <SchemaFields schema={globalSchema} namePrefix="global_data" />
              </>
            )}
            <Button
              type="primary"
              size="large"
              block
              htmlType="submit"
              icon={<PlayCircleOutlined />}
              loading={creating}
              disabled={!canStart}
            >
              确认并开始执行
            </Button>
          </Form>
        </aside>

        <section className="execution-preparation__canvas">
          <div className="execution-canvas-toolbar">
            <div>
              <Text strong>已发布流程</Text>
              <Text type="secondary">此处仅用于核对，运行开始后按该版本执行</Text>
            </div>
            <Tag color="geekblue">v{workflow.published_version || '—'}</Tag>
          </div>
          <WorkflowCanvas
            nodes={definition.nodes}
            edges={definition.edges}
            readonly
            fitView
          />
        </section>

        <aside className="execution-preparation__summary">
          <Text className="execution-eyebrow">BEFORE START</Text>
          <Title level={4}>执行前核对</Title>
          <Descriptions
            column={1}
            size="small"
            items={[
              {
                key: 'workflow',
                label: '流程',
                children: workflow.name,
              },
              {
                key: 'category',
                label: '类别',
                children: categoryNameOf(workflow),
              },
              {
                key: 'version',
                label: '发布版本',
                children: `v${workflow.published_version || '—'}`,
              },
              {
                key: 'mode',
                label: '运行模式',
                children: '正式运行',
              },
            ]}
          />
          <Divider />
          <div className="execution-preparation__steps">
            <div>
              <CheckCircleOutlined />
              <span><strong>已选择流程</strong><small>当前使用不可变的已发布版本</small></span>
            </div>
            <div>
              <FileSearchOutlined />
              <span><strong>补齐运行信息</strong><small>检验编号会用于文件匹配和后续审计</small></span>
            </div>
            <div>
              <ApartmentOutlined />
              <span><strong>进入执行工作台</strong><small>创建后可随时从“执行记录”返回</small></span>
            </div>
          </div>
          <Paragraph type="secondary" className="execution-preparation__note">
            返回流程目录不会产生空白记录；开始执行后，输入内容将锁定并保存在运行快照中。
          </Paragraph>
        </aside>
      </main>
    </div>
  );
}
