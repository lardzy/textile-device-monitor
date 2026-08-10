import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  Empty,
  List,
  Result,
  Segmented,
  Skeleton,
  Space,
  Tag,
  Typography,
} from 'antd';
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  InboxOutlined,
  ReloadOutlined,
  UserOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { getHumanTask, getHumanTasks } from '../../api/execution';
import ExecutionChrome from './ExecutionChrome';
import HumanTaskCard from './HumanTaskCard';
import './execution.css';

const { Text, Title } = Typography;
const ACTIVE_STATUSES = new Set(['open', 'pending', 'claimed']);

const taskOf = payload => payload?.task || payload?.human_task || payload;
const nodeRunOf = payload => payload?.node_run || payload?.nodeRun || taskOf(payload)?.node_run;

const statusMeta = {
  open: { label: '待处理', color: 'orange' },
  pending: { label: '待处理', color: 'orange' },
  claimed: { label: '处理中', color: 'processing' },
  completed: { label: '已完成', color: 'success' },
  rejected: { label: '已驳回', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
};

const INBOX_POLL_INTERVAL_MS = 10000;

export default function ExecutionTaskInbox() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const [filter, setFilter] = useState('active');
  const [tasks, setTasks] = useState([]);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState(null);
  const [detailError, setDetailError] = useState(null);

  const loadTasks = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) {
      setLoading(true);
    }
    try {
      setTasks(await getHumanTasks());
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (id, { quiet = false } = {}) => {
    if (!id) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    if (!quiet) {
      setDetailLoading(true);
    }
    try {
      setDetail(await getHumanTask(id));
      setDetailError(null);
    } catch (requestError) {
      setDetail(null);
      setDetailError(requestError);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    loadDetail(taskId);
  }, [loadDetail, taskId]);

  // 收件箱常驻期间静默轮询，新任务和状态变化无需退出重进即可看到。
  // 表单本地未提交的修改由 HumanTaskCard 按 revision 保护，静默刷新不会覆盖。
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.hidden) {
        return;
      }
      loadTasks({ quiet: true });
      if (taskId) {
        loadDetail(taskId, { quiet: true });
      }
    }, INBOX_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadTasks, loadDetail, taskId]);

  const visibleTasks = useMemo(() => tasks.filter(task => (
    filter === 'active' ? ACTIVE_STATUSES.has(task.status) : !ACTIVE_STATUSES.has(task.status)
  )), [filter, tasks]);
  const activeCount = tasks.filter(task => ACTIVE_STATUSES.has(task.status)).length;
  const selectedTask = detail ? taskOf(detail) : null;
  const selectedNodeRun = detail ? nodeRunOf(detail) : null;

  const refreshAll = async () => {
    await Promise.all([
      loadTasks({ quiet: true }),
      taskId ? loadDetail(taskId, { quiet: true }) : Promise.resolve(),
    ]);
  };

  return (
    <div className="execution-page execution-task-inbox">
      <ExecutionChrome
        title="人工任务收件箱"
        subtitle="集中处理分配给您的文件选择、复核输入与发布确认，新任务会自动出现"
        backTo={{ path: '/execution', label: '流程目录' }}
        actions={(
          <Button icon={<ReloadOutlined />} onClick={refreshAll}>刷新</Button>
        )}
      />

      {error && (
        <Alert
          showIcon
          type="error"
          message="人工任务加载失败"
          description={error.message}
          action={<Button size="small" onClick={() => loadTasks()}>重试</Button>}
        />
      )}

      <main className="execution-task-inbox__grid">
        <section className="execution-task-inbox__list">
          <div className="execution-task-inbox__list-header">
            <div>
              <Text className="execution-eyebrow">HUMAN TASKS</Text>
              <Title level={4}>
                待处理任务 <Badge count={activeCount} overflowCount={99} />
              </Title>
            </div>
            <Segmented
              value={filter}
              onChange={setFilter}
              options={[
                { value: 'active', label: '待处理' },
                { value: 'closed', label: '已结束' },
              ]}
            />
          </div>
          {loading ? (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Skeleton active />
              <Skeleton active />
            </Space>
          ) : (
            <List
              dataSource={visibleTasks}
              locale={{ emptyText: filter === 'active' ? '当前没有待处理任务' : '暂无已结束任务' }}
              renderItem={(task) => {
                const meta = statusMeta[task.status] || { label: task.status, color: 'default' };
                const selected = String(task.id) === String(taskId);
                return (
                  <List.Item
                    className={`execution-task-inbox__item ${selected ? 'is-selected' : ''}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => navigate(`/execution/tasks/${task.id}`)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        navigate(`/execution/tasks/${task.id}`);
                      }
                    }}
                  >
                    <List.Item.Meta
                      avatar={task.status === 'completed'
                        ? <CheckCircleOutlined className="is-completed" />
                        : <ClockCircleOutlined className="is-pending" />}
                      title={(
                        <Space size={8}>
                          <span>{task.title || '人工处理'}</span>
                          <Tag color={meta.color}>{meta.label}</Tag>
                        </Space>
                      )}
                      description={(
                        <Space direction="vertical" size={2}>
                          <span>{task.inspection_number || task.run?.inspection_number || `运行 ${task.run_id}`}</span>
                          <span>
                            <UserOutlined /> {task.candidate_role
                              ? `角色：${task.candidate_role}`
                              : task.assigned_user_id ? '已定向分配' : '运行发起人'}
                            {' · '}
                            {task.created_at ? dayjs(task.created_at).format('MM-DD HH:mm') : ''}
                          </span>
                        </Space>
                      )}
                    />
                  </List.Item>
                );
              }}
            />
          )}
        </section>

        <section className="execution-task-inbox__detail">
          {!taskId ? (
            <Empty
              image={<InboxOutlined />}
              description="从左侧选择一项任务查看并处理"
            />
          ) : detailLoading ? (
            <Skeleton active />
          ) : detailError ? (
            <Result
              status={detailError.status === 404 ? '404' : 'warning'}
              title={detailError.status === 404 ? '任务不存在或您无权处理' : '任务详情加载失败'}
              subTitle={detailError.message}
              extra={<Button onClick={() => loadDetail(taskId)}>重试</Button>}
            />
          ) : selectedTask ? (
            <>
              <div className="execution-task-inbox__context">
                <Text className="execution-eyebrow">TASK CONTEXT</Text>
                <Title level={4}>
                  {detail?.workflow?.name || detail?.run?.workflow_name || '执行流程人工步骤'}
                </Title>
                <Space wrap>
                  <Tag>{detail?.run?.inspection_number || selectedTask.inspection_number || '未提供检验编号'}</Tag>
                  <Tag>运行 {selectedTask.run_id}</Tag>
                  {selectedTask.due_at && (
                    <Tag color="warning">截止 {dayjs(selectedTask.due_at).format('MM-DD HH:mm')}</Tag>
                  )}
                </Space>
              </div>
              <HumanTaskCard
                task={selectedTask}
                nodeRun={selectedNodeRun}
                inspectionNumber={detail?.run?.inspection_number
                  || selectedTask.inspection_number}
                onChanged={refreshAll}
              />
              {!selectedNodeRun && selectedTask.status !== 'completed' && (
                <Alert
                  showIcon
                  type="warning"
                  message="任务上下文尚未返回"
                  description="文件选择任务需要服务端详情接口返回候选资料，刷新后再处理。"
                />
              )}
            </>
          ) : null}
        </section>
      </main>
    </div>
  );
}
