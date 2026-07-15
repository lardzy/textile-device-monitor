import {
  CloseCircleOutlined,
  CopyOutlined,
  DownloadOutlined,
  ExclamationCircleOutlined,
  EyeOutlined,
  PlusOutlined,
  ReloadOutlined,
  RetweetOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  DatePicker,
  Empty,
  Input,
  message,
  Modal,
  Progress,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { areaApi } from '../../api/area';
import NewAreaJobDrawer from './NewAreaJobDrawer';
import {
  ACTIVE_JOB_STATUSES,
  formatAreaDateTime,
  formatJobDuration,
  getAreaErrorMessage,
  JOB_STATUS_META,
} from './areaUtils';

const { RangePicker } = DatePicker;

const STATUS_FILTERS = {
  all: undefined,
  active: 'queued,running,cancelling',
  completed: 'succeeded',
  attention: 'succeeded_with_errors,failed,cancelled',
};

function AreaTaskCenter() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [modelFilter, setModelFilter] = useState();
  const [dateRange, setDateRange] = useState(null);
  const [modelOptions, setModelOptions] = useState([]);
  const [systemStatus, setSystemStatus] = useState(null);
  const [newJobOpen, setNewJobOpen] = useState(false);
  const [highlightedJobId, setHighlightedJobId] = useState('');

  const loadJobs = useCallback(async ({ nextPage = page, quiet = false } = {}) => {
    if (!quiet) setLoading(true);
    try {
      const payload = await areaApi.listJobs({
        page: nextPage,
        page_size: 20,
        limit: 1000,
        q: query || undefined,
        status: STATUS_FILTERS[statusFilter],
        model: modelFilter || undefined,
        created_from: dateRange?.[0]?.startOf('day').toISOString(),
        created_to: dateRange?.[1]?.endOf('day').toISOString(),
      });
      setJobs(payload?.items || []);
      setTotal(Number(payload?.total || 0));
      setPage(Number(payload?.page || nextPage));
    } catch (error) {
      message.error(getAreaErrorMessage(error, '任务列表加载失败'));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [dateRange, modelFilter, page, query, statusFilter]);

  const loadPageContext = useCallback(async () => {
    const [configResult, statusResult] = await Promise.allSettled([
      areaApi.getConfig(),
      areaApi.getStatus(),
    ]);
    if (configResult.status === 'fulfilled') {
      setModelOptions(configResult.value?.model_options || []);
    }
    if (statusResult.status === 'fulfilled') {
      setSystemStatus(statusResult.value);
    }
  }, []);

  useEffect(() => {
    loadPageContext();
  }, [loadPageContext]);

  useEffect(() => {
    loadJobs({ nextPage: 1 });
  }, [query, statusFilter, modelFilter, dateRange]); // eslint-disable-line react-hooks/exhaustive-deps

  const hasActiveJobs = jobs.some((job) => ACTIVE_JOB_STATUSES.includes(job.status));
  useEffect(() => {
    if (!hasActiveJobs) return undefined;
    const timer = window.setInterval(() => loadJobs({ quiet: true }), 2500);
    return () => window.clearInterval(timer);
  }, [hasActiveJobs, loadJobs]);

  const handleCancel = (job) => {
    Modal.confirm({
      title: job.status === 'queued' ? '取消排队任务' : '停止正在处理的任务',
      icon: <ExclamationCircleOutlined />,
      content: job.status === 'queued'
        ? `确定取消 ${job.folder_name} 的任务吗？`
        : '当前图片处理完成后任务将停止，已处理结果不会作为正式结果导出。',
      okText: '确认取消',
      okButtonProps: { danger: true },
      cancelText: '返回',
      async onOk() {
        try {
          await areaApi.cancelJob(job.job_id);
          message.success(job.status === 'queued' ? '任务已取消' : '已提交停止请求');
          await loadJobs({ quiet: true });
        } catch (error) {
          message.error(getAreaErrorMessage(error));
          throw error;
        }
      },
    });
  };

  const handleRetry = (job) => {
    Modal.confirm({
      title: '重新提交任务',
      content: `将使用当前全局配置为“${job.folder_name}”创建一个新任务，原任务保持不变。`,
      okText: '创建重试任务',
      cancelText: '取消',
      async onOk() {
        try {
          const created = await areaApi.retryJob(job.job_id);
          setHighlightedJobId(created.job_id);
          setStatusFilter('all');
          setPage(1);
          message.success('重试任务已提交');
          await loadJobs({ nextPage: 1, quiet: true });
        } catch (error) {
          message.error(getAreaErrorMessage(error));
          throw error;
        }
      },
    });
  };

  const columns = [
    {
      title: '数据目录',
      dataIndex: 'folder_name',
      width: 190,
      ellipsis: true,
      render: (value, row) => (
        <div className="area-primary-cell">
          <Typography.Text strong ellipsis={{ tooltip: value }}>{value}</Typography.Text>
          <Space size={4}>
            <Typography.Text type="secondary" className="area-job-id">
              {String(row.job_id || '').slice(0, 10)}
            </Typography.Text>
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              title="复制任务 ID"
              onClick={(event) => {
                event.stopPropagation();
                navigator.clipboard?.writeText(row.job_id);
              }}
            />
          </Space>
        </div>
      ),
    },
    { title: '模型', dataIndex: 'model_name', width: 130, ellipsis: true, responsive: ['lg'] },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (status, row) => {
        const meta = JOB_STATUS_META[status] || { label: status, color: 'default' };
        return (
          <Space size={4}>
            <Tag color={meta.color}>{meta.label}</Tag>
            {row.error_code ? (
              <Tooltip title={getAreaErrorMessage(row.error_code, row.error_message)}>
                <ExclamationCircleOutlined className="area-error-icon" />
              </Tooltip>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: '处理进度',
      key: 'progress',
      width: 170,
      render: (_, row) => {
        const totalImages = Math.max(0, Number(row.total_images || 0));
        const processed = Math.max(0, Number(row.processed_images || 0));
        const percent = totalImages ? Math.round((processed / totalImages) * 100) : 0;
        const progressText = `${processed}/${totalImages || '-'} · 成功 ${Number(row.succeeded_images || 0)} · 失败 ${Number(row.failed_images || 0)}`;
        return (
          <div className="area-progress-cell">
            <Progress percent={percent} size="small" showInfo={false} status={row.status === 'failed' ? 'exception' : 'normal'} />
            <Typography.Text type="secondary" ellipsis={{ tooltip: progressText }}>
              {progressText}
            </Typography.Text>
          </div>
        );
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 160,
      responsive: ['xl'],
      render: formatAreaDateTime,
    },
    {
      title: '耗时',
      key: 'duration',
      width: 90,
      responsive: ['xl'],
      render: (_, row) => formatJobDuration(row),
    },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 130,
      render: (_, row) => (
        <Space size={2} onClick={(event) => event.stopPropagation()}>
          <Tooltip title="查看任务">
            <Button type="text" icon={<EyeOutlined />} onClick={() => navigate(`/tools/area/jobs/${row.job_id}`)} />
          </Tooltip>
          <Tooltip title="导出 Excel">
            <Button
              type="text"
              icon={<DownloadOutlined />}
              disabled={!['succeeded', 'succeeded_with_errors'].includes(row.status)}
              href={areaApi.getExcelUrl(row.job_id)}
              target="_blank"
            />
          </Tooltip>
          {ACTIVE_JOB_STATUSES.includes(row.status) ? (
            <Tooltip title="取消任务">
              <Button danger type="text" icon={<CloseCircleOutlined />} onClick={() => handleCancel(row)} />
            </Tooltip>
          ) : null}
          {['failed', 'cancelled', 'succeeded_with_errors'].includes(row.status) ? (
            <Tooltip title="重新提交">
              <Button type="text" icon={<RetweetOutlined />} onClick={() => handleRetry(row)} />
            </Tooltip>
          ) : null}
        </Space>
      ),
    },
  ];

  const applySearch = () => {
    setQuery(queryInput.trim());
    setPage(1);
  };

  return (
    <div className="area-page area-task-center">
      {systemStatus && !systemStatus.ok ? (
        <Alert
          className="area-system-alert"
          type="error"
          showIcon
          message="面积识别运行环境存在异常"
          description={systemStatus.issues?.map((item) => getAreaErrorMessage(item)).join('；')}
          action={<Button size="small" onClick={() => navigate('/tools/area/settings')}>检查设置</Button>}
        />
      ) : null}

      <div className="area-page-toolbar">
        <Space wrap size={8}>
          <Segmented
            value={statusFilter}
            onChange={(value) => { setStatusFilter(value); setPage(1); }}
            options={[
              { label: '全部', value: 'all' },
              { label: '进行中', value: 'active' },
              { label: '已完成', value: 'completed' },
              { label: '需关注', value: 'attention' },
            ]}
          />
          <Input
            className="area-task-search"
            value={queryInput}
            allowClear
            prefix={<SearchOutlined />}
            placeholder="任务 ID、目录或模型"
            onChange={(event) => setQueryInput(event.target.value)}
            onPressEnter={applySearch}
          />
          <Select
            allowClear
            value={modelFilter}
            placeholder="全部模型"
            options={modelOptions.map((item) => ({ value: item, label: item }))}
            onChange={(value) => { setModelFilter(value); setPage(1); }}
            style={{ width: 150 }}
          />
          <RangePicker
            value={dateRange}
            onChange={(value) => { setDateRange(value); setPage(1); }}
            allowClear
          />
          <Button icon={<ReloadOutlined />} title="刷新任务" loading={loading} onClick={() => loadJobs()} />
        </Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setNewJobOpen(true)}>
          新建任务
        </Button>
      </div>

      <div className="area-table-surface">
        <Table
          rowKey="job_id"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={jobs}
          scroll={{ x: 'max-content' }}
          pagination={{
            current: page,
            pageSize: 20,
            total,
            showSizeChanger: false,
            showTotal: (value) => `共 ${value} 个任务`,
            onChange: (nextPage) => loadJobs({ nextPage }),
          }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务" /> }}
          rowClassName={(row) => (row.job_id === highlightedJobId ? 'area-row-highlighted' : '')}
          onRow={(row) => ({
            onDoubleClick: () => navigate(`/tools/area/jobs/${row.job_id}`),
          })}
        />
      </div>

      <NewAreaJobDrawer
        open={newJobOpen}
        modelOptions={modelOptions}
        onClose={() => setNewJobOpen(false)}
        onCreated={(job) => {
          setNewJobOpen(false);
          setHighlightedJobId(job.job_id);
          setStatusFilter('all');
          setPage(1);
          loadJobs({ nextPage: 1, quiet: true });
        }}
      />
    </div>
  );
}

export default AreaTaskCenter;
