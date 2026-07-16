import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Empty,
  Progress,
  Segmented,
  Select,
  Skeleton,
  Table,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  DashboardOutlined,
  DownloadOutlined,
  FieldTimeOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { saveAs } from 'file-saver';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useNavigate } from 'react-router-dom';

import { deviceApi } from '../api/devices';
import { statsApi } from '../api/stats';
import { DISPLAY_TIMEZONE, displayNow, toDisplayDayjs } from '../utils/dateHelper';
import './analytics.css';

const { RangePicker } = DatePicker;
const MAX_STATS_RANGE_DAYS = 366;

const STATUS_COLORS = {
  idle: '#52c41a',
  busy: '#1677ff',
  maintenance: '#fa8c16',
  error: '#ff4d4f',
  offline: '#98a2b3',
};

const STAT_TYPE_OPTIONS = [
  { label: '按日', value: 'daily' },
  { label: '按周', value: 'weekly' },
  { label: '按月', value: 'monthly' },
];

const TREND_METRICS = {
  completed_tasks: { label: '完成任务', color: '#1677ff', unit: '项' },
  utilization_rate: { label: '利用率', color: '#52c41a', unit: '%' },
  avg_duration_seconds: { label: '平均耗时', color: '#fa8c16', unit: '秒' },
};

const COMPARISON_METRICS = {
  completed_tasks: { label: '完成量', color: '#1677ff', unit: '项' },
  utilization_rate: { label: '利用率', color: '#52c41a', unit: '%' },
  avg_duration: { label: '平均耗时', color: '#fa8c16', unit: '秒' },
};

const formatDuration = (value) => {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return '0 秒';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
};

const getUtilizationColor = (value) => {
  const numeric = Number(value || 0);
  if (numeric >= 70) return '#52c41a';
  if (numeric >= 35) return '#1677ff';
  return '#faad14';
};

const getPeriodLabel = (value, statType) => {
  const parsed = toDisplayDayjs(value);
  if (!parsed.isValid()) return '-';
  if (statType === 'monthly') return parsed.format('YYYY-MM');
  if (statType === 'weekly') return `${parsed.format('MM-DD')} 周`;
  return parsed.format('MM-DD');
};

const escapeCsvValue = (value) => {
  let text = value == null ? '' : String(value);
  if (/^[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
};

const normalizeSummaryItem = (item) => {
  const totalTasks = Number(item?.total_tasks || 0);
  const completedTasks = Number(item?.completed_tasks || 0);
  const cohortStartedTasks = Number(item?.cohort_started_tasks ?? totalTasks);
  const cohortCompletedTasks = Number(
    item?.cohort_completed_tasks ?? Math.min(completedTasks, cohortStartedTasks),
  );
  const fallbackRate = cohortStartedTasks > 0
    ? (cohortCompletedTasks / cohortStartedTasks) * 100
    : 0;
  const completionRate = Number(item?.completion_rate ?? fallbackRate);
  return {
    ...item,
    total_tasks: totalTasks,
    completed_tasks: completedTasks,
    cohort_started_tasks: cohortStartedTasks,
    cohort_completed_tasks: cohortCompletedTasks,
    completion_rate: Number.isFinite(completionRate)
      ? Math.max(0, Math.min(100, completionRate))
      : 0,
    avg_duration: Number(item?.avg_duration || 0),
    max_duration: Number(item?.max_duration || 0),
    min_duration: Number(item?.min_duration || 0),
    utilization_rate: Number(item?.utilization_rate || item?.utilization || 0),
  };
};

function KpiCard({ icon, iconColor, iconBackground, label, value, suffix, note, tooltip }) {
  return (
    <Card className="analytics-kpi-card">
      <div className="analytics-kpi-card__icon" style={{ color: iconColor, background: iconBackground }}>
        {icon}
      </div>
      <div className="analytics-kpi-card__content">
        <Tooltip title={tooltip}>
          <div className="analytics-kpi-card__label">{label}</div>
        </Tooltip>
        <div className="analytics-kpi-card__value">
          {value}<span className="analytics-kpi-card__suffix">{suffix}</span>
        </div>
        <div className="analytics-kpi-card__note">{note}</div>
      </div>
    </Card>
  );
}

function Statistics() {
  const navigate = useNavigate();
  const [devices, setDevices] = useState([]);
  const [realtime, setRealtime] = useState({});
  const [summary, setSummary] = useState([]);
  const [trend, setTrend] = useState([]);
  const [dateRange, setDateRange] = useState(() => [displayNow().subtract(6, 'day'), displayNow()]);
  const [statType, setStatType] = useState('daily');
  const [selectedDeviceId, setSelectedDeviceId] = useState(undefined);
  const [trendMetric, setTrendMetric] = useState('completed_tasks');
  const [comparisonMetric, setComparisonMetric] = useState('completed_tasks');
  const [loading, setLoading] = useState(false);
  const [errorText, setErrorText] = useState('');
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [lastUpdatedAt, setLastUpdatedAt] = useState(null);
  const requestIdRef = useRef(0);

  const datePresets = useMemo(() => [
    { label: '今天', value: [displayNow(), displayNow()] },
    { label: '最近 7 天', value: [displayNow().subtract(6, 'day'), displayNow()] },
    { label: '最近 30 天', value: [displayNow().subtract(29, 'day'), displayNow()] },
    { label: '本月', value: [displayNow().startOf('month'), displayNow()] },
  ], []);

  const deviceMap = useMemo(
    () => new Map(devices.map(device => [device.id, device])),
    [devices],
  );

  const loadDevices = useCallback(async () => {
    try {
      const payload = await deviceApi.getAll();
      setDevices(Array.isArray(payload) ? payload : []);
    } catch (error) {
      setDevices([]);
    }
  }, []);

  const loadStatistics = useCallback(async () => {
    if (!dateRange?.[0] || !dateRange?.[1]) return;
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setErrorText('');

    const params = {
      stat_type: statType,
      start_date: dateRange[0].format('YYYY-MM-DD'),
      end_date: dateRange[1].format('YYYY-MM-DD'),
    };
    const trendParams = selectedDeviceId ? { ...params, device_id: selectedDeviceId } : params;

    const [realtimeResult, summaryResult, trendResult] = await Promise.allSettled([
      statsApi.getRealtime(),
      statsApi.getSummary(params),
      statsApi.getTrend(trendParams),
    ]);

    if (requestIdRef.current !== requestId) return;

    const failedSections = [];
    if (realtimeResult.status === 'fulfilled') {
      setRealtime(realtimeResult.value || {});
    } else {
      setRealtime({});
      failedSections.push('当前设备状态');
    }

    if (summaryResult.status === 'fulfilled') {
      const normalized = (Array.isArray(summaryResult.value) ? summaryResult.value : [])
        .map(normalizeSummaryItem);
      setSummary(normalized);
    } else {
      setSummary([]);
      failedSections.push('设备汇总');
    }

    if (trendResult.status === 'fulfilled') {
      const items = Array.isArray(trendResult.value?.items) ? trendResult.value.items : [];
      setTrend(items.map(item => ({
        ...item,
        period_label: getPeriodLabel(item.bucket_start || item.period_start, statType),
        completed_tasks: Number(item.completed_tasks || 0),
        utilization_rate: Number(item.utilization_rate || 0),
        avg_duration_seconds: Number(item.avg_duration_seconds || 0),
      })));
    } else {
      setTrend([]);
      failedSections.push('时间趋势');
    }

    if (failedSections.length) {
      setErrorText(`${failedSections.join('、')}加载失败，页面保留了其余可用数据。`);
    }
    if (failedSections.length < 3) setLastUpdatedAt(displayNow());
    setLoading(false);
  }, [dateRange, selectedDeviceId, statType, refreshVersion]);

  useEffect(() => {
    loadDevices();
  }, [loadDevices]);

  useEffect(() => {
    loadStatistics();
    return () => {
      requestIdRef.current += 1;
    };
  }, [loadStatistics]);

  const visibleSummary = useMemo(() => (
    selectedDeviceId
      ? summary.filter(item => Number(item.device_id) === Number(selectedDeviceId))
      : summary
  ), [selectedDeviceId, summary]);

  const totals = useMemo(() => {
    const started = visibleSummary.reduce((sum, item) => sum + item.total_tasks, 0);
    const completed = visibleSummary.reduce((sum, item) => sum + item.completed_tasks, 0);
    const cohortStarted = visibleSummary.reduce(
      (sum, item) => sum + item.cohort_started_tasks,
      0,
    );
    const cohortCompleted = visibleSummary.reduce(
      (sum, item) => sum + item.cohort_completed_tasks,
      0,
    );
    const completionRate = cohortStarted > 0
      ? (cohortCompleted / cohortStarted) * 100
      : 0;
    const durationWeight = visibleSummary.reduce((sum, item) => sum + item.completed_tasks, 0);
    const avgDuration = durationWeight > 0
      ? visibleSummary.reduce((sum, item) => sum + item.avg_duration * item.completed_tasks, 0) / durationWeight
      : 0;
    const avgUtilization = visibleSummary.length > 0
      ? visibleSummary.reduce((sum, item) => sum + item.utilization_rate, 0) / visibleSummary.length
      : 0;
    return {
      started,
      completed,
      cohortStarted,
      cohortCompleted,
      completionRate,
      avgDuration,
      avgUtilization,
    };
  }, [visibleSummary]);

  const statusData = useMemo(() => {
    const maintenance = Number(realtime.maintenance_devices || 0);
    const error = Number(realtime.error_devices || 0);
    const busy = Number(realtime.busy_devices || 0);
    const online = Number(realtime.online_devices || 0);
    const idleFallback = Math.max(0, online - busy - maintenance - error);
    return [
      { key: 'idle', name: '空闲', value: Number(realtime.idle_devices ?? idleFallback) },
      { key: 'busy', name: '检测中', value: busy },
      { key: 'maintenance', name: '维护', value: maintenance },
      { key: 'error', name: '故障', value: error },
      { key: 'offline', name: '离线', value: Number(realtime.offline_devices || 0) },
    ];
  }, [realtime]);

  const comparisonData = useMemo(() => (
    visibleSummary
      .map(item => ({
        ...item,
        device_name: item.device_name || deviceMap.get(item.device_id)?.name || `设备 ${item.device_id}`,
      }))
      .sort((left, right) => Number(right[comparisonMetric] || 0) - Number(left[comparisonMetric] || 0))
  ), [comparisonMetric, deviceMap, visibleSummary]);

  const handleExport = () => {
    if (!visibleSummary.length) return;
    const headers = [
      '设备名称',
      '设备编号',
      '开始任务',
      '完成任务',
      '完成率计入开始任务',
      '完成率计入完成任务',
      '完成率(%)',
      '平均耗时(秒)',
      '最长耗时(秒)',
      '最短耗时(秒)',
      '利用率(%)',
    ];
    const rows = visibleSummary.map(item => {
      const device = deviceMap.get(item.device_id);
      return [
        item.device_name || device?.name || `设备 ${item.device_id}`,
        device?.device_code || '',
        item.total_tasks,
        item.completed_tasks,
        item.cohort_started_tasks,
        item.cohort_completed_tasks,
        item.completion_rate.toFixed(2),
        item.avg_duration,
        item.max_duration,
        item.min_duration,
        item.utilization_rate.toFixed(2),
      ];
    });
    const csv = `\ufeff${[headers, ...rows].map(row => row.map(escapeCsvValue).join(',')).join('\r\n')}`;
    saveAs(
      new Blob([csv], { type: 'text/csv;charset=utf-8' }),
      `device_statistics_${dateRange[0].format('YYYYMMDD')}_${dateRange[1].format('YYYYMMDD')}.csv`,
    );
  };

  const tableColumns = useMemo(() => [
    {
      title: '设备',
      dataIndex: 'device_name',
      fixed: 'left',
      width: 210,
      render: (value, record) => {
        const device = deviceMap.get(record.device_id);
        return (
          <div className="history-device-cell">
            <div className="history-device-cell__name">{value || device?.name || `设备 ${record.device_id}`}</div>
            <div className="history-device-cell__meta">{device?.device_code || `ID ${record.device_id}`}</div>
          </div>
        );
      },
    },
    { title: '开始任务', dataIndex: 'total_tasks', width: 105, sorter: (a, b) => a.total_tasks - b.total_tasks },
    { title: '完成任务', dataIndex: 'completed_tasks', width: 105, sorter: (a, b) => a.completed_tasks - b.completed_tasks },
    {
      title: (
        <Tooltip title="按范围内开始的任务计算；跨范围完成只计入完成量，不计入完成率分子">
          完成率
        </Tooltip>
      ),
      key: 'completion_rate',
      width: 115,
      sorter: (a, b) => a.completion_rate - b.completion_rate,
      render: (_, record) => `${record.completion_rate.toFixed(1)}%`,
    },
    {
      title: '平均耗时',
      dataIndex: 'avg_duration',
      width: 135,
      sorter: (a, b) => a.avg_duration - b.avg_duration,
      render: formatDuration,
    },
    {
      title: '耗时范围',
      key: 'duration_range',
      width: 180,
      render: (_, record) => `${formatDuration(record.min_duration)} – ${formatDuration(record.max_duration)}`,
    },
    {
      title: '设备利用率',
      dataIndex: 'utilization_rate',
      width: 220,
      sorter: (a, b) => a.utilization_rate - b.utilization_rate,
      render: value => (
        <div className="analytics-utilization-cell">
          <Progress percent={Math.max(0, Math.min(100, Number(value || 0)))} showInfo={false} strokeColor={getUtilizationColor(value)} />
          <span className="analytics-utilization-cell__value">{Number(value || 0).toFixed(1)}%</span>
        </div>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 105,
      render: (_, record) => (
        <Button type="link" size="small" onClick={() => navigate(`/history?device_id=${record.device_id}`)}>
          查看历史
        </Button>
      ),
    },
  ], [deviceMap, navigate]);

  const currentTrendMetric = TREND_METRICS[trendMetric];
  const currentComparisonMetric = COMPARISON_METRICS[comparisonMetric];
  const hasTrendData = trend.some(item => Number(item[trendMetric] || 0) > 0);
  const hasStatusData = statusData.some(item => item.value > 0);
  const onlineCount = Number(realtime.online_devices || 0);
  const totalDevices = Number(realtime.total_devices || 0);

  return (
    <div className="analytics-page">
      <div className="analytics-page__hero">
        <div>
          <span className="analytics-page__eyebrow">OPERATIONS ANALYTICS</span>
          <Typography.Title level={3} className="analytics-page__title">设备运行与产能分析</Typography.Title>
          <Typography.Text className="analytics-page__subtitle">
            从任务完成量、检测耗时和忙碌时长三个维度评估设备运行效率。
          </Typography.Text>
        </div>
        <div className="analytics-page__actions">
          <Button icon={<DownloadOutlined />} disabled={!visibleSummary.length} onClick={handleExport}>导出明细</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => setRefreshVersion(value => value + 1)}>刷新</Button>
        </div>
      </div>

      <Card className="analytics-filter-card analytics-stat-filter">
        <div className="analytics-stat-filter__controls">
          <div className="analytics-stat-filter__item analytics-stat-filter__item--range">
            <span className="analytics-stat-filter__label">统计日期</span>
            <RangePicker
              value={dateRange}
              presets={datePresets}
              allowClear={false}
              disabledDate={current => current && current > displayNow().endOf('day')}
              onChange={(value) => {
                if (!value?.[0] || !value?.[1]) return;
                const rangeDays = value[1].startOf('day').diff(
                  value[0].startOf('day'),
                  'day',
                ) + 1;
                if (rangeDays > MAX_STATS_RANGE_DAYS) {
                  message.warning(`统计日期范围最多支持 ${MAX_STATS_RANGE_DAYS} 天`);
                  return;
                }
                setDateRange(value);
              }}
            />
          </div>
          <div className="analytics-stat-filter__item">
            <span className="analytics-stat-filter__label">统计粒度</span>
            <Segmented value={statType} options={STAT_TYPE_OPTIONS} onChange={setStatType} />
          </div>
          <div className="analytics-stat-filter__item analytics-stat-filter__item--device">
            <span className="analytics-stat-filter__label">设备范围</span>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              value={selectedDeviceId}
              placeholder="全部设备"
              onChange={setSelectedDeviceId}
              options={devices.map(device => ({
                value: device.id,
                label: `${device.name}${device.device_code ? ` · ${device.device_code}` : ''}`,
              }))}
            />
          </div>
          <div className="analytics-filter-meta">
            <span>口径：{DISPLAY_TIMEZONE}</span>
            {lastUpdatedAt ? <span>· 更新于 {lastUpdatedAt.format('HH:mm:ss')}</span> : null}
          </div>
        </div>
      </Card>

      {errorText ? (
        <Alert
          type="warning"
          showIcon
          message="部分统计数据暂不可用"
          description={errorText}
          action={<Button size="small" onClick={() => setRefreshVersion(value => value + 1)}>重试</Button>}
        />
      ) : null}

      {loading && !summary.length ? (
        <Card className="analytics-panel"><Skeleton active paragraph={{ rows: 6 }} /></Card>
      ) : (
        <>
          <div className="analytics-kpi-grid">
            <KpiCard
              icon={<CheckCircleOutlined />}
              iconColor="#1677ff"
              iconBackground="#eaf3ff"
              label="完成任务数"
              value={totals.completed}
              suffix="项"
              note={`共开始 ${totals.started} 项任务`}
              tooltip="所选日期范围内写入任务完成事件的数量"
            />
            <KpiCard
              icon={<DashboardOutlined />}
              iconColor="#52c41a"
              iconBackground="#edf9f0"
              label="任务完成率"
              value={totals.completionRate.toFixed(1)}
              suffix="%"
              note={`本范围开始 ${totals.cohortStarted} 项，范围内完成 ${totals.cohortCompleted} 项`}
              tooltip="按所选范围内开始的任务作为分母；只有同一任务也在该范围内完成时计入分子"
            />
            <KpiCard
              icon={<ClockCircleOutlined />}
              iconColor="#fa8c16"
              iconBackground="#fff5e8"
              label="平均检测耗时"
              value={formatDuration(totals.avgDuration)}
              suffix=""
              note="按已完成任务加权"
              tooltip="基于历史完成记录中的任务耗时计算"
            />
            <KpiCard
              icon={<FieldTimeOutlined />}
              iconColor="#722ed1"
              iconBackground="#f4edff"
              label="平均设备利用率"
              value={totals.avgUtilization.toFixed(1)}
              suffix="%"
              note="忙碌时长占统计窗口比例"
              tooltip="各设备忙碌时长占所选时间窗口的平均值"
            />
          </div>

          <Card
            className="analytics-panel analytics-chart-card"
            title={(
              <div className="analytics-panel__title">
                <span>时间趋势</span>
                <span className="analytics-panel__hint">按{STAT_TYPE_OPTIONS.find(item => item.value === statType)?.label.slice(1)}聚合</span>
              </div>
            )}
            extra={(
              <Segmented
                size="small"
                value={trendMetric}
                onChange={setTrendMetric}
                options={Object.entries(TREND_METRICS).map(([value, config]) => ({ value, label: config.label }))}
              />
            )}
          >
            {hasTrendData ? (
              <ResponsiveContainer width="100%" height={320}>
                <LineChart data={trend} accessibilityLayer margin={{ top: 10, right: 26, left: 4, bottom: 4 }}>
                  <CartesianGrid stroke="#edf1f5" strokeDasharray="4 4" vertical={false} />
                  <XAxis dataKey="period_label" tickLine={false} axisLine={{ stroke: '#dfe5ec' }} />
                  <YAxis
                    tickLine={false}
                    axisLine={false}
                    domain={trendMetric === 'utilization_rate' ? [0, 100] : [0, 'auto']}
                    tickFormatter={value => trendMetric === 'utilization_rate' ? `${value}%` : value}
                  />
                  <ChartTooltip formatter={value => [`${Number(value || 0).toFixed(trendMetric === 'completed_tasks' ? 0 : 1)} ${currentTrendMetric.unit}`, currentTrendMetric.label]} />
                  <Line
                    type="monotone"
                    dataKey={trendMetric}
                    name={currentTrendMetric.label}
                    stroke={currentTrendMetric.color}
                    strokeWidth={3}
                    dot={{ r: 3, fill: '#fff', strokeWidth: 2 }}
                    activeDot={{ r: 5 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="analytics-chart-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前范围没有趋势数据" /></div>
            )}
          </Card>

          <div className="analytics-chart-grid">
            <Card
              className="analytics-panel analytics-chart-card"
              title={<div className="analytics-panel__title"><span>设备横向对比</span><span className="analytics-panel__hint">按指标从高到低排序</span></div>}
              extra={(
                <Segmented
                  size="small"
                  value={comparisonMetric}
                  onChange={setComparisonMetric}
                  options={Object.entries(COMPARISON_METRICS).map(([value, config]) => ({ value, label: config.label }))}
                />
              )}
            >
              {comparisonData.length ? (
                <div className="analytics-chart-scroll">
                  <ResponsiveContainer width="100%" height={Math.max(300, comparisonData.length * 46)}>
                    <BarChart data={comparisonData} layout="vertical" accessibilityLayer margin={{ top: 4, right: 34, left: 20, bottom: 4 }}>
                      <CartesianGrid stroke="#edf1f5" strokeDasharray="4 4" horizontal={false} />
                      <XAxis
                        type="number"
                        tickLine={false}
                        axisLine={{ stroke: '#dfe5ec' }}
                        domain={comparisonMetric === 'utilization_rate' ? [0, 100] : [0, 'auto']}
                        tickFormatter={value => comparisonMetric === 'utilization_rate' ? `${value}%` : value}
                      />
                      <YAxis type="category" dataKey="device_name" width={120} tickLine={false} axisLine={false} />
                      <ChartTooltip formatter={value => [`${Number(value || 0).toFixed(comparisonMetric === 'completed_tasks' ? 0 : 1)} ${currentComparisonMetric.unit}`, currentComparisonMetric.label]} />
                      <Bar dataKey={comparisonMetric} name={currentComparisonMetric.label} fill={currentComparisonMetric.color} radius={[0, 5, 5, 0]} maxBarSize={22} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <div className="analytics-chart-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前范围没有设备统计" /></div>
              )}
            </Card>

            <Card
              className="analytics-panel analytics-chart-card"
              title={<div className="analytics-panel__title"><span>当前设备状态</span><span className="analytics-panel__hint">实时快照</span></div>}
            >
              {hasStatusData ? (
                <div className="analytics-donut-wrap">
                  <ResponsiveContainer width="100%" height={300}>
                    <PieChart accessibilityLayer>
                      <Pie data={statusData} dataKey="value" nameKey="name" cx="50%" cy="44%" innerRadius={68} outerRadius={96} paddingAngle={2}>
                        {statusData.map(item => <Cell key={item.key} fill={STATUS_COLORS[item.key]} />)}
                      </Pie>
                      <ChartTooltip formatter={(value, name) => [`${value} 台`, name]} />
                      <Legend verticalAlign="bottom" iconType="circle" />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="analytics-donut-center">
                    <strong>{onlineCount}</strong>
                    <span>在线 / {totalDevices}</span>
                  </div>
                </div>
              ) : (
                <div className="analytics-chart-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无设备状态" /></div>
              )}
            </Card>
          </div>

          <Card
            className="analytics-panel analytics-table-card"
            title={<div className="analytics-panel__title"><span>设备统计明细</span><span className="analytics-panel__hint">精确数据与排序</span></div>}
            extra={<div className="analytics-table-summary">共 {visibleSummary.length} 台设备</div>}
          >
            <Table
              rowKey="device_id"
              size="middle"
              loading={loading}
              dataSource={visibleSummary}
              columns={tableColumns}
              scroll={{ x: 1180 }}
              pagination={false}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前范围没有统计数据" /> }}
            />
          </Card>
        </>
      )}
    </div>
  );
}

export default Statistics;
