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
  Collapse,
  Divider,
  Form,
  Result,
  Space,
  Spin,
  message,
} from 'antd';
import { PlayCircleOutlined } from '@ant-design/icons';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  startExecutionRun,
  getExecutionWorkflow,
} from '../../api/execution';
import { normalizeWorkflowDefinition } from '../../utils/executionWorkflow';
import ExecutionChrome from './ExecutionChrome';
import SchemaFields from './SchemaFields';
import WorkflowCanvas from './WorkflowCanvas';
import './execution.css';

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

const availabilityOf = workflow => (
  workflow?.is_enabled !== false
  && !workflow?.archived_at
  && !workflow?.replacement_pending
  && workflow?.availability?.available !== false
  && Boolean(workflow?.published_version)
);

export default function ExecutionRunPreparation({
  initialWorkflow = null,
  inspectionNumber,
  onStarted,
} = {}) {
  const { workflowId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialNumberRef = useRef(inspectionNumber ?? searchParams.get('number') ?? '');
  const embedded = Boolean(initialWorkflow);
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const [workflow, setWorkflow] = useState(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const creatingRef = useRef(false);
  const [error, setError] = useState(null);

  const loadWorkflow = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setWorkflow(initialWorkflow || await getExecutionWorkflow(workflowId));
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [initialWorkflow, workflowId]);

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
  const globalSchema = useMemo(() => workflow?.global_schema
    || definition.global_schema
    || { type: 'object', properties: {} }, [workflow?.global_schema, definition.global_schema]);
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
    if (embedded) return;
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
    const targetNumber = values.input_data?.target_sample_number?.trim();
    const inputData = {
      ...(values.input_data || {}),
      inspection_number: normalizedNumber,
    };
    if (targetNumber) {
      inputData.target_sample_number = targetNumber;
    }
    const payload = {
      workflow_id: workflow.id,
      inspection_number: normalizedNumber,
      ...(targetNumber ? { target_sample_number: targetNumber } : {}),
      input_data: inputData,
      global_data: values.global_data || {},
    };
    if (creatingRef.current || !canStart) return;
    creatingRef.current = true;
    setCreating(true);
    try {
      const run = await startExecutionRun(payload);
      if (onStarted) onStarted(run);
      else navigate(`/execution/runs/${run.id || run.run_id}`, { replace: true });
    } catch (requestError) {
      message.error(requestError.message || '创建执行任务失败');
    } finally {
      creatingRef.current = false;
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
    <div className={`execution-preparation${embedded ? ' execution-preparation--embedded' : ''}`}>
      {!embedded && <ExecutionChrome
        title={workflow.name || '执行准备'}
        subtitle="填写本次任务信息后开始执行"
        backTo={{ path: '/execution', label: '流程目录' }}
      />}

      {workflow.archived_at && (
        <Alert
          banner showIcon type="info" message="该流程已归档"
          description="历史运行和人工待办仍可继续访问。新任务请使用接替流程。"
          action={workflow.replacement_workflow?.is_enabled && (
            <Button onClick={() => navigate(`/execution/workflows/${workflow.replacement_workflow.id}/start?${searchParams.toString()}`)}>前往新流程</Button>
          )}
        />
      )}
      {!canStart && !workflow.archived_at && (
        <Alert
          banner
          showIcon
          type="warning"
          message="当前流程暂不可运行"
          description={workflow.availability?.message || '流程未发布或已停用。'}
        />
      )}

      <main className="execution-preparation__form">
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
            开始执行
          </Button>
        </Form>
        {!embedded && <Collapse style={{ marginTop: 24 }} items={[{
          key: 'definition',
          label: `查看流程 · v${workflow.published_version || '—'}`,
          children: <div style={{ height: 360 }}>
          <WorkflowCanvas
            nodes={definition.nodes}
            edges={definition.edges}
            readonly
            fitView
          />
          </div>,
        }]} />}
      </main>
    </div>
  );
}
