import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  message,
  Modal,
  Progress,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  DownloadOutlined,
  FileImageOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  UndoOutlined,
} from '@ant-design/icons';
import { saveAs } from 'file-saver';
import { useSearchParams } from 'react-router-dom';

import { deviceApi } from '../api/devices';
import { historyApi } from '../api/history';
import ResultsModal from '../components/ResultsModal';
import {
  displayNow,
  formatDateTime,
  parseDisplayDate,
  toDisplayDayjs,
} from '../utils/dateHelper';
import ResultsImages from './ResultsImages';
import './analytics.css';

const { RangePicker } = DatePicker;

const STATUS_OPTIONS = [
  { label: '空闲', value: 'idle', color: 'success' },
  { label: '检测中', value: 'busy', color: 'processing' },
  { label: '维护中', value: 'maintenance', color: 'warning' },
  { label: '故障', value: 'error', color: 'error' },
  { label: '离线', value: 'offline', color: 'default' },
];

const STATUS_MAP = Object.fromEntries(STATUS_OPTIONS.map(item => [item.value, item]));

const createDefaultFormValues = (deviceId) => ({
  device_id: deviceId,
  keyword: '',
  dateRange: [displayNow().subtract(6, 'day').startOf('day'), displayNow().endOf('day')],
});

const createInitialFormValues = (searchParams) => {
  const deviceId = Number(searchParams.get('device_id'));
  const startDate = parseDisplayDate(searchParams.get('start_date'));
  const endDate = parseDisplayDate(searchParams.get('end_date'));
  const defaults = createDefaultFormValues(Number.isInteger(deviceId) && deviceId > 0 ? deviceId : undefined);
  const keyword = searchParams.get('keyword');
  if (keyword) defaults.keyword = keyword;
  if (startDate.isValid() && endDate.isValid()) defaults.dateRange = [startDate, endDate];
  return defaults;
};

const toApiFilters = (values) => {
  const filters = {};
  if (values?.device_id) filters.device_id = values.device_id;
  if (values?.keyword?.trim()) filters.keyword = values.keyword.trim();
  if (values?.dateRange?.[0] && values?.dateRange?.[1]) {
    const startDate = parseDisplayDate(values.dateRange[0].format('YYYY-MM-DD'));
    const endDate = parseDisplayDate(values.dateRange[1].format('YYYY-MM-DD'));
    filters.start_date = startDate.startOf('day').toISOString();
    filters.end_date = endDate.add(1, 'day').startOf('day').toISOString();
  }
  return filters;
};

const formatDuration = (value) => {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return '-';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const restSeconds = Math.round(seconds % 60);
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return restSeconds > 0 ? `${minutes} 分 ${restSeconds} 秒` : `${minutes} 分钟`;
};

const getOutputPath = (record) => (
  record?.device_metrics?.olympus?.output_path
  || record?.device_metrics?.output_path
  || ''
);

const getPathBasename = (value) => {
  const normalized = String(value || '').replace(/\\/g, '/').replace(/\/+$/, '');
  return normalized ? normalized.split('/').pop() : '';
};

const getResultFolder = (record) => getPathBasename(getOutputPath(record)) || record?.task_name || '';

const getResultTablePageUrl = (record, folder) => {
  if (!record?.device_id) return '';
  const folderParam = folder ? `&folder=${encodeURIComponent(folder)}` : '';
  return `/results/table?device_id=${record.device_id}${folderParam}`;
};

const getErrorMessage = (error, fallback) => {
  if (error?.status === 404) return '当前筛选条件下没有可用数据';
  if (error?.status === 422) return '筛选条件不正确，请检查后重试';
  return error?.message && error.message !== 'Network Error' ? error.message : fallback;
};

function HistoryQuery() {
  const [form] = Form.useForm();
  const [searchParams, setSearchParams] = useSearchParams();
  const [initialFormValues] = useState(() => createInitialFormValues(searchParams));
  const [devices, setDevices] = useState([]);
  const [data, setData] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [errorText, setErrorText] = useState('');
  const [filters, setFilters] = useState(() => toApiFilters(initialFormValues));
  const [pagination, setPagination] = useState(() => {
    const page = Number(searchParams.get('page'));
    const pageSize = Number(searchParams.get('page_size'));
    return {
      page: Number.isInteger(page) && page > 0 ? page : 1,
      pageSize: [20, 50, 100].includes(pageSize) ? pageSize : 20,
    };
  });
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [tableModal, setTableModal] = useState({ open: false, record: null, folder: '' });
  const [imagesModal, setImagesModal] = useState({ open: false, record: null, folder: '' });
  const [detailRecord, setDetailRecord] = useState(null);
  const [imagesLayoutVersion, setImagesLayoutVersion] = useState(0);
  const requestIdRef = useRef(0);

  const deviceMap = useMemo(
    () => new Map(devices.map(device => [device.id, device])),
    [devices],
  );

  const datePresets = useMemo(() => [
    { label: '今天', value: [displayNow().startOf('day'), displayNow().endOf('day')] },
    { label: '最近 7 天', value: [displayNow().subtract(6, 'day').startOf('day'), displayNow().endOf('day')] },
    { label: '最近 30 天', value: [displayNow().subtract(29, 'day').startOf('day'), displayNow().endOf('day')] },
    { label: '本月', value: [displayNow().startOf('month'), displayNow().endOf('day')] },
  ], []);

  const fetchDevices = useCallback(async () => {
    try {
      const payload = await deviceApi.getAll();
      setDevices(Array.isArray(payload) ? payload : []);
    } catch (error) {
      message.warning('设备名称加载失败，历史记录仍可按设备编号显示');
    }
  }, []);

  const fetchHistory = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setErrorText('');
    try {
      const response = await historyApi.get({
        ...filters,
        page: pagination.page,
        page_size: pagination.pageSize,
      });
      if (requestIdRef.current !== requestId) return;
      setData(Array.isArray(response?.data) ? response.data : []);
      setTotal(Number(response?.total || 0));
    } catch (error) {
      if (requestIdRef.current !== requestId) return;
      setData([]);
      setTotal(0);
      setErrorText(getErrorMessage(error, '历史记录加载失败，请检查网络连接后重试'));
    } finally {
      if (requestIdRef.current === requestId) setLoading(false);
    }
  }, [filters, pagination.page, pagination.pageSize, refreshVersion]);

  useEffect(() => {
    fetchDevices();
  }, [fetchDevices]);

  useEffect(() => {
    fetchHistory();
    return () => {
      requestIdRef.current += 1;
    };
  }, [fetchHistory]);

  const syncSearchParams = useCallback((nextFilters, nextPagination) => {
    const params = {};
    if (nextFilters.device_id) params.device_id = String(nextFilters.device_id);
    if (nextFilters.keyword) params.keyword = nextFilters.keyword;
    if (nextFilters.start_date) {
      params.start_date = toDisplayDayjs(nextFilters.start_date).format('YYYY-MM-DD');
    }
    if (nextFilters.end_date) {
      params.end_date = toDisplayDayjs(nextFilters.end_date).subtract(1, 'millisecond').format('YYYY-MM-DD');
    }
    if (nextPagination.page > 1) params.page = String(nextPagination.page);
    if (nextPagination.pageSize !== 20) params.page_size = String(nextPagination.pageSize);
    setSearchParams(params, { replace: true });
  }, [setSearchParams]);

  const handleSearch = (values) => {
    const nextFilters = toApiFilters(values);
    const nextPagination = { ...pagination, page: 1 };
    setFilters(nextFilters);
    setPagination(nextPagination);
    syncSearchParams(nextFilters, nextPagination);
  };

  const handleReset = () => {
    const defaults = createDefaultFormValues();
    const nextFilters = toApiFilters(defaults);
    const nextPagination = { page: 1, pageSize: pagination.pageSize };
    form.setFieldsValue(defaults);
    setFilters(nextFilters);
    setPagination(nextPagination);
    syncSearchParams(nextFilters, nextPagination);
  };

  const handleExport = async () => {
    setExporting(true);
    const messageKey = 'history-export';
    message.loading({ content: '正在生成历史记录文件…', key: messageKey, duration: 0 });
    try {
      const blob = await historyApi.export(filters);
      saveAs(blob, `device_history_${displayNow().format('YYYYMMDD_HHmmss')}.xlsx`);
      message.success({ content: '导出完成', key: messageKey });
    } catch (error) {
      message.error({ content: getErrorMessage(error, '导出失败，请稍后重试'), key: messageKey });
    } finally {
      setExporting(false);
    }
  };

  const openTableResult = useCallback((record) => {
    setTableModal({ open: true, record, folder: getResultFolder(record) });
  }, []);

  const openImageResult = useCallback((record) => {
    setImagesModal({ open: true, record, folder: getResultFolder(record) });
  }, []);

  const columns = useMemo(() => [
    {
      title: '完成时间',
      dataIndex: 'reported_at',
      width: 172,
      fixed: 'left',
      render: value => <span className="history-duration">{formatDateTime(value)}</span>,
    },
    {
      title: '设备',
      dataIndex: 'device_id',
      width: 190,
      render: (deviceId) => {
        const device = deviceMap.get(deviceId);
        return (
          <div className="history-device-cell">
            <div className="history-device-cell__name">{device?.name || `设备 ${deviceId}`}</div>
            <div className="history-device-cell__meta">
              {[device?.device_code, device?.location].filter(Boolean).join(' · ') || `ID ${deviceId}`}
            </div>
          </div>
        );
      },
    },
    {
      title: '任务',
      dataIndex: 'task_name',
      width: 230,
      render: (taskName, record) => (
        <div className="history-task-cell">
          <div className="history-task-cell__name">{taskName || '未命名任务'}</div>
          <div className="history-task-cell__meta">
            {record.task_id ? (
              <Typography.Text type="secondary" copyable={{ text: record.task_id }}>
                {record.task_id}
              </Typography.Text>
            ) : '无任务 ID'}
          </div>
        </div>
      ),
    },
    {
      title: '检测耗时',
      dataIndex: 'task_duration_seconds',
      width: 125,
      render: value => (
        <Tooltip title={Number.isFinite(Number(value)) ? `${Number(value)} 秒` : null}>
          <span className="history-duration">{formatDuration(value)}</span>
        </Tooltip>
      ),
    },
    {
      title: '结果目录',
      key: 'result_path',
      width: 235,
      render: (_, record) => {
        const outputPath = getOutputPath(record);
        const folder = getResultFolder(record);
        if (!folder) return <Typography.Text type="secondary">未记录</Typography.Text>;
        return (
          <Tooltip title={outputPath || folder}>
            <Typography.Text className="history-result-path" copyable={{ text: outputPath || folder }}>
              {folder}
            </Typography.Text>
          </Tooltip>
        );
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      fixed: 'right',
      render: (_, record) => {
        const folder = getResultFolder(record);
        const isConfocal = record?.device_metrics?.device_type === 'laser_confocal';
        return folder ? (
          <Space size={2} onClick={event => event.stopPropagation()}>
            <Button type="link" size="small" icon={<InfoCircleOutlined />} onClick={() => setDetailRecord(record)}>
              详情
            </Button>
            {!isConfocal ? (
              <Button type="link" size="small" icon={<FileTextOutlined />} onClick={() => openTableResult(record)}>
                表格
              </Button>
            ) : null}
            <Button type="link" size="small" icon={<FileImageOutlined />} onClick={() => openImageResult(record)}>
              图片
            </Button>
          </Space>
        ) : (
          <Button type="link" size="small" icon={<InfoCircleOutlined />} onClick={(event) => {
            event.stopPropagation();
            setDetailRecord(record);
          }}>
            详情
          </Button>
        );
      },
    },
  ], [deviceMap, openImageResult, openTableResult]);

  const activeFilterCount = [filters.device_id, filters.keyword, filters.start_date].filter(Boolean).length;
  const tableRecord = tableModal.record;
  const imagesRecord = imagesModal.record;

  return (
    <div className="analytics-page">
      <div className="analytics-page__hero">
        <div>
          <span className="analytics-page__eyebrow">TASK TRACEABILITY</span>
          <Typography.Title level={3} className="analytics-page__title">任务与结果追溯</Typography.Title>
          <Typography.Text className="analytics-page__subtitle">
            按设备、任务和完成日期快速定位检测记录，并直接回看对应结果文件。
          </Typography.Text>
        </div>
        <div className="analytics-page__actions">
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRefreshVersion(value => value + 1)}>
            刷新
          </Button>
          <Button
            type="primary"
            icon={<DownloadOutlined />}
            loading={exporting}
            disabled={loading || total === 0}
            onClick={handleExport}
          >
            导出 Excel
          </Button>
        </div>
      </div>

      <Card className="analytics-filter-card">
        <Form form={form} layout="vertical" initialValues={initialFormValues} onFinish={handleSearch}>
          <Row gutter={[16, 0]} align="bottom">
            <Col xs={24} sm={12} xl={6}>
              <Form.Item name="device_id" label="设备">
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="全部设备"
                  options={devices.map(device => ({
                    value: device.id,
                    label: `${device.name}${device.device_code ? ` · ${device.device_code}` : ''}`,
                  }))}
                />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Form.Item name="keyword" label="任务关键字">
                <Input allowClear prefix={<SearchOutlined />} placeholder="任务名称或任务 ID" />
              </Form.Item>
            </Col>
            <Col xs={24} md={16} xl={8}>
              <Form.Item name="dateRange" label="完成日期">
                <RangePicker allowClear presets={datePresets} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8} xl={4}>
              <div className="analytics-filter-actions">
                <Space>
                  <Button icon={<UndoOutlined />} onClick={handleReset}>重置</Button>
                  <Button type="primary" htmlType="submit" icon={<SearchOutlined />}>查询</Button>
                </Space>
              </div>
            </Col>
          </Row>
        </Form>
      </Card>

      {errorText ? (
        <Alert
          type="error"
          showIcon
          message="历史记录加载失败"
          description={errorText}
          action={<Button size="small" onClick={() => setRefreshVersion(value => value + 1)}>重试</Button>}
        />
      ) : null}

      <Card
        className="analytics-panel analytics-table-card"
        title={(
          <div className="analytics-panel__title">
            <span>历史任务</span>
            <span className="analytics-panel__hint">仅显示已写入系统的完成记录</span>
          </div>
        )}
        extra={(
          <div className="analytics-table-summary">
            <span>筛选条件 {activeFilterCount} 项</span>
            <span>·</span>
            <span>共 <strong>{total}</strong> 条</span>
          </div>
        )}
      >
        <Table
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={data}
          scroll={{ x: 1320 }}
          onRow={record => ({
            onClick: () => setDetailRecord(record),
            style: { cursor: 'pointer' },
          })}
          pagination={{
            current: pagination.page,
            pageSize: pagination.pageSize,
            total,
            pageSizeOptions: ['20', '50', '100'],
            showSizeChanger: true,
            showQuickJumper: total > pagination.pageSize * 3,
            showTotal: value => `共 ${value} 条记录`,
            onChange: (page, pageSize) => {
              const nextPagination = { page, pageSize };
              setPagination(nextPagination);
              syncSearchParams(filters, nextPagination);
            },
          }}
          locale={{
            emptyText: (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的历史任务">
                <Button onClick={handleReset}>清除筛选</Button>
              </Empty>
            ),
          }}
        />
      </Card>

      <Drawer
        open={Boolean(detailRecord)}
        width="min(560px, calc(100vw - 24px))"
        title="任务详情"
        onClose={() => setDetailRecord(null)}
      >
        {detailRecord ? (
          <Descriptions bordered column={1} size="small">
            <Descriptions.Item label="完成时间">{formatDateTime(detailRecord.reported_at)}</Descriptions.Item>
            <Descriptions.Item label="设备">
              {deviceMap.get(detailRecord.device_id)?.name || `设备 ${detailRecord.device_id}`}
              {deviceMap.get(detailRecord.device_id)?.device_code
                ? ` · ${deviceMap.get(detailRecord.device_id).device_code}`
                : ''}
            </Descriptions.Item>
            <Descriptions.Item label="任务名称">{detailRecord.task_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="任务 ID">
              <Typography.Text copyable={detailRecord.task_id ? { text: detailRecord.task_id } : false}>
                {detailRecord.task_id || '-'}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="最终状态">
              <Tag color={(STATUS_MAP[detailRecord.status] || {}).color || 'default'}>
                {(STATUS_MAP[detailRecord.status] || {}).label || detailRecord.status || '未知'}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="完成进度">
              {Number.isFinite(Number(detailRecord.task_progress)) ? (
                <Progress percent={Number(detailRecord.task_progress)} size="small" />
              ) : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="检测耗时">{formatDuration(detailRecord.task_duration_seconds)}</Descriptions.Item>
            <Descriptions.Item label="结果路径">
              <Typography.Paragraph
                style={{ marginBottom: 0, wordBreak: 'break-all' }}
                copyable={getOutputPath(detailRecord) ? { text: getOutputPath(detailRecord) } : false}
              >
                {getOutputPath(detailRecord) || getResultFolder(detailRecord) || '-'}
              </Typography.Paragraph>
            </Descriptions.Item>
          </Descriptions>
        ) : null}
      </Drawer>

      <ResultsModal
        open={tableModal.open}
        title={`${tableRecord?.task_name || '历史任务'} · 结果表格`}
        url={getResultTablePageUrl(tableRecord, tableModal.folder)}
        onClose={() => setTableModal({ open: false, record: null, folder: '' })}
      />

      <Modal
        title={`${imagesRecord?.task_name || '历史任务'} · 结果图片`}
        open={imagesModal.open}
        onCancel={() => setImagesModal({ open: false, record: null, folder: '' })}
        afterOpenChange={(open) => {
          if (open) setImagesLayoutVersion(value => value + 1);
        }}
        footer={null}
        width="90vw"
        style={{ top: 20 }}
        styles={{ body: { height: '80vh', padding: 0, width: '100%', overflow: 'hidden' } }}
        destroyOnClose
      >
        {imagesModal.open && imagesRecord ? (
          <ResultsImages
            deviceId={imagesRecord.device_id}
            folder={imagesModal.folder}
            embedded
            layoutVersion={imagesLayoutVersion}
          />
        ) : null}
      </Modal>
    </div>
  );
}

export default HistoryQuery;
