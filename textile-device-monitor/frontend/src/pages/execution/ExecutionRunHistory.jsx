import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  Alert,
  Button,
  Input,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  EyeOutlined,
  LoadingOutlined,
  ReloadOutlined,
  SearchOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import {
  getExecutionRunsPage,
  getExecutionWorkflows,
} from '../../api/execution';
import ExecutionChrome from './ExecutionChrome';
import './execution.css';

const { Text, Title } = Typography;
const PAGE_SIZE = 20;

const statusMeta = {
  created: { label: '已创建', color: 'default', icon: <ClockCircleOutlined /> },
  pending: { label: '等待执行', color: 'default', icon: <ClockCircleOutlined /> },
  queued: { label: '已排队', color: 'processing', icon: <ClockCircleOutlined /> },
  running: { label: '执行中', color: 'processing', icon: <LoadingOutlined spin /> },
  waiting_human: { label: '等待人工处理', color: 'warning', icon: <ClockCircleOutlined /> },
  paused: { label: '已暂停', color: 'warning', icon: <ClockCircleOutlined /> },
  cancel_pending: { label: '正在安全取消', color: 'warning', icon: <ClockCircleOutlined /> },
  failure_pending: { label: '失败收尾中', color: 'error', icon: <WarningOutlined /> },
  completed: { label: '已完成', color: 'success', icon: <CheckCircleOutlined /> },
  succeeded: { label: '已完成', color: 'success', icon: <CheckCircleOutlined /> },
  failed: { label: '执行失败', color: 'error', icon: <WarningOutlined /> },
  cancelled: { label: '已取消', color: 'default', icon: <ClockCircleOutlined /> },
};

const terminalStatuses = new Set(['completed', 'succeeded', 'failed', 'cancelled']);

const actionLabel = (status) => {
  if (status === 'waiting_human') {
    return '继续处理';
  }
  if (terminalStatuses.has(status)) {
    return '查看结果';
  }
  return '查看进度';
};

const timeLabel = run => (
  run.updated_at
  || run.started_at
  || run.created_at
);

export default function ExecutionRunHistory() {
  const navigate = useNavigate();
  const [scope, setScope] = useState('active');
  const [status, setStatus] = useState();
  const [workflowId, setWorkflowId] = useState();
  const [workflowRows, setWorkflowRows] = useState([]);
  const [workflowOptionsLoading, setWorkflowOptionsLoading] = useState(true);
  const [workflowOptionsFailed, setWorkflowOptionsFailed] = useState(false);
  const [numberInput, setNumberInput] = useState('');
  const [inspectionNumber, setInspectionNumber] = useState('');
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const loadRuns = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) {
      setLoading(true);
    }
    try {
      const result = await getExecutionRunsPage({
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        status_group: scope === 'all' ? undefined : scope,
        status: status || undefined,
        workflow_id: workflowId || undefined,
        inspection_number: inspectionNumber || undefined,
      });
      setRows(result.items);
      setTotal(result.total);
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [inspectionNumber, page, scope, status, workflowId]);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  const loadWorkflowOptions = useCallback(async () => {
    setWorkflowOptionsLoading(true);
    try {
      setWorkflowRows(await getExecutionWorkflows());
      setWorkflowOptionsFailed(false);
    } catch {
      setWorkflowOptionsFailed(true);
    } finally {
      setWorkflowOptionsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadWorkflowOptions();
  }, [loadWorkflowOptions]);

  const applyNumberSearch = () => {
    setPage(1);
    setInspectionNumber(numberInput.trim());
  };

  const workflowOptions = useMemo(
    () => workflowRows.map(workflow => ({
      value: workflow.id || workflow.workflow_id,
      label: [
        workflow.category?.name || workflow.category_name,
        workflow.name || workflow.title || '未命名流程',
      ].filter(Boolean).join(' · '),
    })).filter(option => Boolean(option.value)),
    [workflowRows],
  );

  const columns = useMemo(() => [
    {
      title: '状态',
      dataIndex: 'status',
      width: 150,
      render: value => (
        <Tag
          color={statusMeta[value]?.color || 'default'}
          icon={statusMeta[value]?.icon}
        >
          {statusMeta[value]?.label || value}
        </Tag>
      ),
    },
    {
      title: '检验编号',
      dataIndex: 'inspection_number',
      width: 190,
      render: value => <Text strong>{value || '—'}</Text>,
    },
    {
      title: '执行流程',
      dataIndex: 'workflow_name',
      ellipsis: true,
      render: (value, run) => (
        <div className="execution-run-history__workflow">
          <span>{value || '未命名流程'}</span>
          <small>
            {run.mode === 'test' ? '测试运行' : '正式运行'}
            {run.created_by?.display_name
              ? ` · ${run.created_by.display_name}`
              : ''}
          </small>
        </div>
      ),
    },
    {
      title: '节点进度',
      key: 'progress',
      width: 130,
      render: (_, run) => {
        const completed = Number(run.node_progress?.completed || 0);
        const totalNodes = Number(run.node_progress?.total || 0);
        return (
          <Space size={6}>
            <span>{totalNodes ? `${completed}/${totalNodes}` : '—'}</span>
            {Number(run.open_human_task_count || 0) > 0 && (
              <Tag color="warning">{run.open_human_task_count} 项待办</Tag>
            )}
          </Space>
        );
      },
    },
    {
      title: '最近更新',
      key: 'updated_at',
      width: 170,
      render: (_, run) => (
        timeLabel(run)
          ? dayjs(timeLabel(run)).format('YYYY-MM-DD HH:mm')
          : '—'
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 120,
      fixed: 'right',
      render: (_, run) => (
        <Button
          type="link"
          icon={<EyeOutlined />}
          onClick={() => navigate(`/execution/runs/${run.id}`)}
        >
          {actionLabel(run.status)}
        </Button>
      ),
    },
  ], [navigate]);

  return (
    <div className="execution-page execution-run-history">
      <ExecutionChrome
        title="执行记录"
        subtitle="找回正在执行、等待人工处理及已经结束的流程"
        backTo={{ path: '/execution', label: '流程目录' }}
        actions={(
          <Button
            icon={<ReloadOutlined />}
            onClick={() => {
              loadRuns({ quiet: true });
              loadWorkflowOptions();
            }}
          >
            刷新
          </Button>
        )}
      />

      {error && (
        <Alert
          showIcon
          type="error"
          message="执行记录加载失败"
          description={error.message}
          action={<Button size="small" onClick={() => loadRuns()}>重试</Button>}
        />
      )}

      <main className="execution-run-history__body">
        <section className="execution-run-history__intro">
          <div>
            <Text className="execution-eyebrow">RUN RECORDS</Text>
            <Title level={3}>继续进行中的工作，或查看历史结果</Title>
            <Text type="secondary">
              “待办任务”只处理人工步骤；这里保存每次流程运行的完整入口。
            </Text>
          </div>
          <Segmented
            value={scope}
            onChange={(value) => {
              setScope(value);
              setStatus(undefined);
              setPage(1);
            }}
            options={[
              { value: 'active', label: '进行中' },
              { value: 'all', label: '全部记录' },
              { value: 'terminal', label: '已结束' },
            ]}
          />
        </section>

        <section className="execution-run-history__filters">
          <Input
            allowClear
            value={numberInput}
            prefix={<SearchOutlined />}
            placeholder="按检验编号查找"
            aria-label="按检验编号查找"
            onChange={event => setNumberInput(event.target.value)}
            onPressEnter={applyNumberSearch}
          />
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            value={workflowId}
            loading={workflowOptionsLoading}
            status={workflowOptionsFailed ? 'warning' : undefined}
            placeholder={workflowOptionsFailed
              ? '流程列表加载失败，刷新重试'
              : '全部流程'}
            aria-label="执行流程"
            onChange={(value) => {
              setWorkflowId(value);
              setPage(1);
            }}
            options={workflowOptions}
            notFoundContent={workflowOptionsLoading
              ? '正在加载流程…'
              : '没有可筛选的流程'}
          />
          <Select
            allowClear
            value={status}
            placeholder="全部状态"
            aria-label="运行状态"
            onChange={(value) => {
              setStatus(value);
              if (value) {
                setScope('all');
              }
              setPage(1);
            }}
            options={Object.entries(statusMeta).map(([value, meta]) => ({
              value,
              label: meta.label,
            }))}
          />
          <Button type="primary" icon={<SearchOutlined />} onClick={applyNumberSearch}>
            查询
          </Button>
          {(inspectionNumber || status || workflowId) && (
            <Button
              onClick={() => {
                setNumberInput('');
                setInspectionNumber('');
                setStatus(undefined);
                setWorkflowId(undefined);
                setPage(1);
              }}
            >
              清除条件
            </Button>
          )}
        </section>

        <Table
          rowKey="id"
          className="execution-run-history__table"
          dataSource={rows}
          columns={columns}
          loading={loading}
          scroll={{ x: 980 }}
          locale={{
            emptyText: scope === 'active'
              ? '当前没有进行中的执行流程'
              : '没有符合条件的执行记录',
          }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            showSizeChanger: false,
            showTotal: value => `共 ${value} 条`,
            onChange: setPage,
          }}
        />
      </main>
    </div>
  );
}
