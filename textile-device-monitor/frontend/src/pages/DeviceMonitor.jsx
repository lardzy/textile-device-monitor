import { useCallback, useState, useEffect, useMemo, useRef } from 'react';
import { DndProvider, useDrag, useDrop } from 'react-dnd';
import { HTML5Backend } from 'react-dnd-html5-backend';
import { Alert, Badge, Button, Card, Checkbox, Collapse, Dropdown, Empty, Form, Input, InputNumber, List, message, Modal, Popconfirm, Progress, Select, Skeleton, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { CheckCircleOutlined, ClockCircleFilled, ClockCircleOutlined, DeleteOutlined, DownOutlined, ExclamationCircleOutlined, FileImageOutlined, FileTextOutlined, FolderOpenOutlined, HolderOutlined, LoadingOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, StopOutlined } from '@ant-design/icons';
import { deviceApi } from '../api/devices';
import { queueApi } from '../api/queue';
import { resultsApi } from '../api/results';
import ResultsModal from '../components/ResultsModal';
import ResultsImages from './ResultsImages';
import wsClient from '../websocket/client';
import { formatRelativeTime, formatDateTime, formatTime } from '../utils/dateHelper';
import { addQueueNoticeEntry, getDeviceId, getInspectorName, getOrCreateQueueUserId, getQueueNoticeEntries, getQueueNoticeModes, removeQueueNoticeEntry, saveDeviceId, saveInspectorName, saveQueueNoticeModes } from '../utils/localStorage';
import { buildCompletionNoticeClaim, buildQueueTurnNoticeClaim, claimNotificationOnce } from '../utils/notificationDedup';
import { getQueueSnapshotSignature, queueRecordIdEquals, resolveStableQueueDrop } from '../utils/queueDrag';
import './analytics.css';
import './device-monitor.css';


const statusConfig = {
  idle: { color: 'success', icon: <CheckCircleOutlined />, text: '空闲' },
  busy: { color: 'processing', icon: <LoadingOutlined />, text: '检测中' },
  maintenance: { color: 'warning', icon: <ClockCircleOutlined />, text: '维护中' },
  error: { color: 'error', icon: <ExclamationCircleOutlined />, text: '故障' },
  offline: { color: 'default', icon: <StopOutlined />, text: '离线' },
};

const olympusStateLabels = {
  StateIdle: '空闲',
  StateRepeatStarting: '开始采集',
  StateRepeatRunning: '采集中',
  StateRepeatStopping: '停止中',
};

const getOlympusStateLabel = (state) => {
  if (!state) return '-';
  return olympusStateLabels[state] || state;
};

const getOlympusDisplayState = (olympus, deviceStatus) => {
  if (!olympus) return '-';
  if (olympus.active) return '采集中';
  if (olympus.image_progress != null && olympus.image_progress > 0 && olympus.image_progress < 100) {
    return '采集中';
  }
  if (olympus.frame_current != null && olympus.frame_current > 0) {
    return '采集中';
  }
  const label = getOlympusStateLabel(olympus.state);
  if (label !== '-') return label;
  if (deviceStatus && statusConfig[deviceStatus]) {
    return statusConfig[deviceStatus].text;
  }
  return '-';
};

const getQueuePositionDisplay = (position) => {
  if (position == null) return '-';
  if (position === 1) return '正在使用';
  return position - 1;
};

const getQueuePositionLabel = (position) => {
  if (position == null) return '-';
  if (position === 1) return '正在使用';
  return `位置 ${position - 1}`;
};

const isUnclaimedPlaceholder = (record) => Boolean(record?.is_placeholder && record?.auto_remove_when_inactive);

const getQueueLogDisplay = (log) => {
  switch (log.change_type) {
    case 'join':
      return {
        color: '#1677ff',
        text: log.new_position == null ? (log.remark || '加入排队') : `加入排队 · ${getQueuePositionLabel(log.new_position)}`,
      };
    case 'position_change':
      return {
        color: '#8c8c8c',
        text: log.old_position == null || log.new_position == null
          ? (log.remark || '排队位置已更新')
          : `调整位置 · ${getQueuePositionLabel(log.old_position)} → ${getQueuePositionLabel(log.new_position)}`,
      };
    case 'complete':
      return { color: '#52c41a', text: '测量完成' };
    case 'leave':
      return { color: '#ff4d4f', text: '离开排队' };
    case 'placeholder_create':
      return { color: '#1677ff', text: log.remark || '系统已自动创建占位人员' };
    case 'placeholder_claim':
      return { color: '#52c41a', text: log.remark || '占位人员已被认领' };
    case 'placeholder_delete':
      return { color: '#ff4d4f', text: log.remark || '占位人员已被删除' };
    case 'placeholder_auto_remove':
      return { color: '#fa8c16', text: log.remark || '占位人员已自动移除' };
    case 'timeout_shift':
      return { color: '#ff4d4f', text: log.remark || '超时未使用设备，已顺延' };
    case 'timeout_extend':
      return { color: '#fa8c16', text: log.remark || '设备超时已延长' };
    default:
      break;
  }

  if (log.new_position === 0) {
    return { color: '#52c41a', text: '测量完成' };
  }
  if (log.new_position === -1) {
    return { color: '#ff4d4f', text: '离开排队' };
  }

  if (log.old_position == null || log.new_position == null) {
    return { color: undefined, text: log.remark || '排队状态已更新' };
  }

  return {
    color: undefined,
    text: `${getQueuePositionLabel(log.old_position)} → ${getQueuePositionLabel(log.new_position)}`,
  };
};

const getActiveQueueEntry = (queueList) => {
  if (!Array.isArray(queueList)) return null;
  return queueList.find(item => item.position === 1) || null;
};

const formatCountdown = (totalSeconds) => {
  if (totalSeconds == null) return '-';
  const safeSeconds = Math.max(0, totalSeconds);
  const minutes = Math.floor(safeSeconds / 60);
  const seconds = safeSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
};

const getQueueTimeoutRemainingSeconds = (device, nowMs) => {
  if (!device?.queue_timeout_deadline_at) return null;
  if (device.status !== 'idle') return null;
  const queueCount = Number(device.queue_count || 0);
  if (!Number.isFinite(queueCount) || queueCount < 2) return null;
  if (!device.queue_timeout_active_id) return null;
  const deadlineMs = new Date(device.queue_timeout_deadline_at).getTime();
  if (!Number.isFinite(deadlineMs)) return null;
  const remainingMs = deadlineMs - nowMs;
  return Math.max(0, Math.floor(remainingMs / 1000));
};

const QueueTimeoutNotice = ({ device, queueCount, compact = false, extending = false, onExtend }) => {
  const [nowMs, setNowMs] = useState(Date.now());
  const effectiveDevice = queueCount == null ? device : { ...device, queue_count: queueCount };
  const remainingSeconds = getQueueTimeoutRemainingSeconds(effectiveDevice, nowMs);

  useEffect(() => {
    if (remainingSeconds == null) return undefined;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [effectiveDevice?.queue_timeout_deadline_at, remainingSeconds == null]);

  if (remainingSeconds == null) return null;

  const extendedCount = Number(effectiveDevice?.queue_timeout_extended_count || 0);
  const remainingExtends = Math.max(0, 3 - extendedCount);
  const canExtend = remainingSeconds > 0 && remainingExtends > 0;
  const warning = remainingSeconds <= 60;

  if (compact) {
    return (
      <span className={`monitor-timeout-compact${warning ? ' monitor-timeout-compact--warning' : ''}`}>
        <ClockCircleOutlined /> {formatCountdown(remainingSeconds)}
      </span>
    );
  }

  return (
    <div className={`monitor-timeout-notice${warning ? ' monitor-timeout-notice--warning' : ''}`}>
      <div className="monitor-timeout-notice__content">
        <ClockCircleOutlined />
        <div>
          <strong>{warning ? '即将触发排队顺延' : '设备空闲等待当前使用人'}</strong>
          <span>
            剩余 <b>{formatCountdown(remainingSeconds)}</b>，还可延长 {remainingExtends} 次
          </span>
        </div>
      </div>
      {onExtend ? (
        <Popconfirm
          title="确认延长 5 分钟？"
          description={`本次延长后还剩 ${Math.max(0, remainingExtends - 1)} 次机会。`}
          okText="确认延长"
          cancelText="取消"
          disabled={!canExtend}
          onConfirm={onExtend}
        >
          <Button size="small" loading={extending} disabled={!canExtend}>
            延长 5 分钟
          </Button>
        </Popconfirm>
      ) : null}
    </div>
  );
};

const DeviceOverviewCard = ({ device, selected, onSelect, onQuickQueue }) => {
  const config = statusConfig[device.status] || statusConfig.offline;
  const visualStatus = statusConfig[device.status] ? device.status : 'offline';
  const confocal = isConfocalDevice(device);
  const olympus = device.metrics?.olympus || {};
  const progressValue = device.task_progress == null ? null : Number(device.task_progress);
  const hasProgress = Number.isFinite(progressValue);
  const showTaskProgress = !['idle', 'maintenance', 'offline'].includes(device.status) && hasProgress;
  const progressStatus = device.status === 'error'
    ? 'exception'
    : progressValue === 100
      ? 'success'
      : 'active';
  const statusText = device.status === 'offline' ? '离线' : config.text;
  const rawQueueCount = Number(device.queue_count || 0);
  const queueCount = Number.isFinite(rawQueueCount) ? Math.max(0, rawQueueCount) : 0;

  return (
    <div className={`monitor-device-card-shell${selected ? ' monitor-device-card-shell--selected' : ''}`}>
      <button
        type="button"
        className={`monitor-device-card monitor-device-card--status-${visualStatus}${selected ? ' monitor-device-card--selected' : ''}${device.status === 'offline' ? ' monitor-device-card--offline' : ''}`}
        aria-pressed={selected}
        aria-label={`选择设备 ${device.name}，当前状态 ${statusText}`}
        onClick={() => onSelect(device.id)}
      >
      <div className="monitor-device-card__header">
        <div className="monitor-device-card__identity">
          <strong>{device.name}</strong>
          <span>{device.device_code || `ID ${device.id}`}</span>
        </div>
        <div className="monitor-device-card__signals">
          <Badge status={config.color} text={statusText} />
          <span className={`monitor-device-card__queue${queueCount > 0 ? ' monitor-device-card__queue--waiting' : ''}`}>
            <small>排队</small><strong>{queueCount}</strong><em>人</em>
          </span>
        </div>
      </div>

      <div className="monitor-device-card__task">
        {showTaskProgress ? (
          <>
            <div className="monitor-device-card__task-title">
              <span>{device.task_name || '当前任务'}</span>
              <b className={device.status === 'error' ? 'monitor-device-card__progress-value--error' : ''}>
                {Math.max(0, Math.min(100, progressValue))}%
              </b>
            </div>
            <Progress
              percent={Math.max(0, Math.min(100, progressValue))}
              status={progressStatus}
              showInfo={false}
              strokeColor={device.status === 'error' ? undefined : progressValue === 100 ? '#389e0d' : '#1677ff'}
              aria-label={`${device.name} 当前任务进度 ${Math.max(0, Math.min(100, progressValue))}%`}
            />
            {confocal && olympus.group_total ? (
              <span className="monitor-device-card__task-note">
                {getOlympusDisplayState(olympus, device.status)} · 组 {olympus.group_completed || 0}/{olympus.group_total}
              </span>
            ) : device.task_elapsed_seconds != null ? (
              <span className="monitor-device-card__task-note">
                已运行 {Math.max(0, Math.floor(device.task_elapsed_seconds / 60))} 分钟
              </span>
            ) : null}
          </>
        ) : device.status === 'idle' ? (
          <div className="monitor-device-card__state-copy monitor-device-card__state-copy--idle">
            <CheckCircleOutlined />
            <span>
              <strong>空闲可用</strong>
              <small>{device.task_name && progressValue === 100 ? `最近完成 ${device.task_name}` : '当前可以安排检测'}</small>
            </span>
          </div>
        ) : device.status === 'maintenance' ? (
          <div className="monitor-device-card__state-copy monitor-device-card__state-copy--maintenance">
            <ClockCircleOutlined />
            <span><strong>维护中</strong><small>暂不建议安排检测任务</small></span>
          </div>
        ) : device.status === 'offline' ? (
          <div className="monitor-device-card__state-copy monitor-device-card__state-copy--offline">
            <StopOutlined />
            <span><strong>设备离线</strong><small>连接已中断，数据可能已过期</small></span>
          </div>
        ) : device.status === 'busy' ? (
          <div className="monitor-device-card__state-copy monitor-device-card__state-copy--busy">
            <LoadingOutlined />
            <span><strong>正在检测</strong><small>等待客户端上报任务进度</small></span>
          </div>
        ) : (
          <div className="monitor-device-card__state-copy monitor-device-card__state-copy--error">
            <ExclamationCircleOutlined />
            <span><strong>设备故障</strong><small>请先检查设备和客户端状态</small></span>
          </div>
        )}
      </div>

      <div className="monitor-device-card__meta">
        <span><small>位置</small>{device.location || '-'}</span>
        <span><small>型号</small>{device.model || '-'}</span>
      </div>
        <div className="monitor-device-card__footer">
          <QueueTimeoutNotice device={device} compact />
        </div>
      </button>
      {selected ? (
        <Button
          className="monitor-device-card__quick-action"
          type="primary"
          size="small"
          icon={<DownOutlined />}
          aria-label={`快速前往 ${device.name} 排队输入`}
          onClick={() => onQuickQueue(device.id)}
        >
          快速排队
        </Button>
      ) : null}
    </div>
  );
};

const type = 'queue-row';

const isConfocalDevice = (device) => {
  if (!device) return false;
  return device.metrics?.device_type === 'laser_confocal' || Boolean(device.metrics?.olympus);
};

const DraggableRow = ({
  recordId,
  recordVersion,
  recordPosition,
  queueSnapshot,
  onDropConfirm,
  isActive,
  children,
  className,
  style,
  ...restProps
}) => {
  const ref = useRef(null);
  const [{ isOver, dropClassName }, drop] = useDrop({
    accept: type,
    collect: (monitor) => {
      const { recordId: dragRecordId } = monitor.getItem() || {};
      if (queueRecordIdEquals(dragRecordId, recordId)) {
        return {};
      }
      return {
        isOver: monitor.isOver(),
        dropClassName: 'drop-over-downward',
      };
    },
    drop: (item) => {
      if (queueRecordIdEquals(item.recordId, recordId)) {
        return;
      }
      onDropConfirm(item, {
        recordId,
        recordVersion,
        recordPosition,
        queueSnapshot,
      });
    },
  });
  const [{ isDragging }, drag] = useDrag({
    type,
    item: {
      recordId,
      recordVersion,
      recordPosition,
      queueSnapshot,
    },
    collect: (monitor) => ({
      isDragging: monitor.isDragging(),
    }),
  });
  drag(drop(ref));
  const rowStyle = {
    ...style,
    cursor: isDragging ? 'grabbing' : 'grab',
    ...(isActive ? { background: '#f6ffed' } : null),
    ...(isDragging ? { opacity: 0.55 } : null),
  };
  const rowClassName = [className, isOver ? dropClassName : ''].filter(Boolean).join(' ');
  return (
    <tr
      {...restProps}
      ref={ref}
      className={rowClassName}
      style={rowStyle}
    >
      {children}
    </tr>
  );
};

const queueTableComponents = {
  body: {
    row: DraggableRow,
  },
};

const DragTable = ({ columns, dataSource, onDropConfirm, onRow, ...props }) => {
  const queueSnapshot = getQueueSnapshotSignature(dataSource);
  const getRowProps = (record, index) => ({
    ...(onRow?.(record, index) || {}),
    recordId: record?.id,
    recordVersion: record?.version,
    recordPosition: record?.position,
    queueSnapshot,
    onDropConfirm,
    isActive: record?.position === 1,
  });

  return (
    <Table
      columns={columns}
      dataSource={dataSource}
      components={queueTableComponents}
      pagination={false}
      size="small"
      onRow={getRowProps}
      {...props}
    />
  );
};

const isTempOutputPath = (path) => {
  // 检查是否为临时输出路径，排除Olympus设备的临时文件地址
  if (!path) return true;
  
  const normalized = path.replace(/\//g, '\\').toLowerCase();
  
  // Olympus设备临时文件路径模式
  const olympusTempPatterns = [
    "programdata\\olympus\\lext-ols50-sw\\microscopeapp\\temp\\image",
    "microscopeapp\\temp\\image",  // 保留原有的检查
    "temp\\image",  // 更宽泛的临时路径检查
  ];
  
  // 检查是否匹配任何临时路径模式
  for (const pattern of olympusTempPatterns) {
    if (normalized.includes(pattern)) {
      return true;
    }
  }
  
  // 检查是否为系统临时目录下的文件
  if (normalized.startsWith("c:\\programdata\\") && normalized.includes("temp")) {
    return true;
  }
  
  if (normalized.startsWith("c:\\windows\\temp\\")) {
    return true;
  }
  
  return false;
};

const getValidOutputPath = (outputPath) => {
  if (!outputPath) return null;
  return isTempOutputPath(outputPath) ? null : outputPath;
};

const getFolderNameFromPath = (path) => {
  if (!path) return '';
  const normalized = String(path).replace(/\\/g, '/').replace(/\/+$/, '');
  if (!normalized) return '';
  const idx = normalized.lastIndexOf('/');
  return idx >= 0 ? normalized.slice(idx + 1) : normalized;
};

const getDefaultRenameName = (folderName) => {
  const safeName = String(folderName || '').trim();
  if (!safeName) return '';
  const [prefix] = safeName.split('_');
  return prefix || safeName;
};

const invalidFolderNamePattern = /[\\/:*?"<>|]/;

const formatTemperature = (value) => {
  if (value == null || value === '') return '-';
  const numeric = Number(value);
  return `${Number.isFinite(numeric) ? numeric : value}°C`;
};

function DeviceMonitor() {
  const [devices, setDevices] = useState([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState(() => {
    const stored = getDeviceId();
    const numeric = stored == null ? null : Number(stored);
    return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
  });
  const [queue, setQueue] = useState([]);
  const [queueLogs, setQueueLogs] = useState([]);
  const [devicesLoading, setDevicesLoading] = useState(true);
  const [devicesError, setDevicesError] = useState('');
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueError, setQueueError] = useState('');
  const [resultsLoading, setResultsLoading] = useState(false);
  const [resultsError, setResultsError] = useState('');
  const [resultsAvailability, setResultsAvailability] = useState('ready');
  const [lastUpdatedAt, setLastUpdatedAt] = useState(null);
  const [queueSubmitting, setQueueSubmitting] = useState(false);
  const [extendingDeviceId, setExtendingDeviceId] = useState(null);
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [deviceTypeFilter, setDeviceTypeFilter] = useState('all');
  const [sortMode, setSortMode] = useState('default');
  const [inspectorName, setInspectorName] = useState(getInspectorName());
  const [tableModal, setTableModal] = useState({ open: false, folder: null });
  const [imagesModal, setImagesModal] = useState({ open: false, folder: null });
  const [imagesLayoutVersion, setImagesLayoutVersion] = useState(0);
  const [claimModal, setClaimModal] = useState({
    open: false,
    record: null,
    submittingAction: null,
  });
  const [cleanupModal, setCleanupModal] = useState({
    open: false,
    folder: null,
    sourceFolderName: '',
    renameEnabled: false,
    newFolderName: '',
    submitting: false,
  });
  const [recentResults, setRecentResults] = useState([]);
  const [notifyModes, setNotifyModes] = useState(() => getQueueNoticeModes());
  const [queueFocusPulse, setQueueFocusPulse] = useState(false);
  const [form] = Form.useForm();
  const [claimForm] = Form.useForm();
  const [modal, modalContextHolder] = Modal.useModal();
  const devicesRef = useRef([]);
  const queueRef = useRef([]);
  const selectedDeviceIdRef = useRef(selectedDeviceId);
  const devicesFetchInFlightRef = useRef(false);
  const devicesLiveRevisionRef = useRef(new Map());
  const queueRequestIdRef = useRef(0);
  const queueSnapshotsByDeviceRef = useRef(new Map());
  const resultsRequestIdRef = useRef(0);
  const queueForegroundRequestIdRef = useRef(null);
  const resultsForegroundRequestIdRef = useRef(null);
  const queueDeviceGenerationRef = useRef(new Map());
  const pendingQueueRefreshRef = useRef(new Map());
  const queueNotifyIntentRef = useRef(new Map());
  const notifyModesRef = useRef(notifyModes);
  const lastProgressRef = useRef(new Map());
  const activeQueueRef = useRef(new Map());
  const deviceNotifyTimersRef = useRef(new Map());
  const queueCompletionTimersRef = useRef(new Map());
  const queueUserIdRef = useRef(getOrCreateQueueUserId());
  const resultsLoadedDeviceIdRef = useRef(null);
  const mountedRef = useRef(true);
  const managedModalHandlesRef = useRef(new Set());
  const queueNoticeModalIdsRef = useRef(new Set());
  const queueFormAnchorRef = useRef(null);
  const queueFocusTimerRef = useRef(null);

  const destroyManagedModals = useCallback(() => {
    managedModalHandlesRef.current.forEach(handle => handle.destroy());
    managedModalHandlesRef.current.clear();
  }, []);

  const openManagedConfirm = useCallback((config) => {
    let handle;
    const originalAfterClose = config.afterClose;
    handle = modal.confirm({
      ...config,
      afterClose: () => {
        managedModalHandlesRef.current.delete(handle);
        originalAfterClose?.();
      },
    });
    managedModalHandlesRef.current.add(handle);
    return handle;
  }, [modal]);


  const selectedDevice = useMemo(() => {
    return devices.find(device => device.id === selectedDeviceId) || null;
  }, [devices, selectedDeviceId]);

  useEffect(() => {
    devicesRef.current = devices;
  }, [devices]);

  useEffect(() => {
    queueRef.current = queue;
  }, [queue]);

  useEffect(() => {
    selectedDeviceIdRef.current = selectedDeviceId;
    if (selectedDeviceId != null) saveDeviceId(selectedDeviceId);
  }, [selectedDeviceId]);

  useEffect(() => {
    notifyModesRef.current = notifyModes;
    saveQueueNoticeModes(notifyModes);
  }, [notifyModes]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      destroyManagedModals();
      deviceNotifyTimersRef.current.forEach(timer => window.clearTimeout(timer));
      deviceNotifyTimersRef.current.clear();
      queueCompletionTimersRef.current.forEach(timer => window.clearTimeout(timer));
      queueCompletionTimersRef.current.clear();
      pendingQueueRefreshRef.current.clear();
      queueNotifyIntentRef.current.clear();
      queueNoticeModalIdsRef.current.clear();
      if (queueFocusTimerRef.current) {
        window.clearTimeout(queueFocusTimerRef.current);
      }
    };
  }, [destroyManagedModals]);

  useEffect(() => {
    form.setFieldsValue({
      inspector_name: inspectorName || getInspectorName(),
      copies: 1,
    });
  }, [form]);

  const fetchDevices = async () => {
    if (devicesFetchInFlightRef.current) return;
    devicesFetchInFlightRef.current = true;
    const liveRevisionsAtStart = new Map(devicesLiveRevisionRef.current);
    if (!devicesRef.current.length) setDevicesLoading(true);
    try {
      const data = await deviceApi.getAll();
      const sorted = (Array.isArray(data) ? data : [])
        .slice()
        .sort((a, b) => {
          const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
          const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
          if (aTime !== bTime) {
            return aTime - bTime;
          }
          return a.id - b.id;
        });
      setDevices(prev => {
        const byId = new Map(prev.map(item => [item.id, item]));
        return sorted.map(item => {
          const existing = byId.get(item.id);
          if (!existing) {
            return item;
          }
          const liveRevisionAtStart = liveRevisionsAtStart.get(item.id) || 0;
          const currentLiveRevision = devicesLiveRevisionRef.current.get(item.id) || 0;
          if (currentLiveRevision !== liveRevisionAtStart) {
            return {
              ...item,
              ...existing,
              queue_count: existing.queue_count ?? item.queue_count,
            };
          }
          return {
            ...existing,
            ...item,
            queue_count: item.queue_count ?? existing.queue_count,
          };
        });
      });
      const currentId = selectedDeviceIdRef.current;
      const nextId = currentId && sorted.some(item => item.id === currentId)
        ? currentId
        : sorted[0]?.id || null;
      selectedDeviceIdRef.current = nextId;
      setSelectedDeviceId(nextId);
      setDevicesError('');
      setLastUpdatedAt(new Date());
    } catch (error) {
      setDevicesError(error?.message || '设备状态加载失败，请检查服务连接');
    } finally {
      devicesFetchInFlightRef.current = false;
      setDevicesLoading(false);
    }
  };

  const fetchQueue = async (deviceId, options = {}) => {
    const {
      notify = false,
      reason,
      notificationContext,
      updateState = true,
      silent = false,
    } = options;
    const isForeground = updateState && !silent;
    if (!deviceId) return;
    if (notify) {
      const pendingIntent = queueNotifyIntentRef.current.get(deviceId);
      queueNotifyIntentRef.current.set(deviceId, {
        notify: true,
        reason: reason || pendingIntent?.reason,
        notificationContext: notificationContext || pendingIntent?.notificationContext,
      });
    }
    if (silent && updateState && queueForegroundRequestIdRef.current != null) {
      const pending = pendingQueueRefreshRef.current.get(deviceId);
      pendingQueueRefreshRef.current.set(deviceId, {
        notify: Boolean(notify || pending?.notify),
        reason: reason || pending?.reason,
        notificationContext: notificationContext || pending?.notificationContext,
      });
      return;
    }
    const deviceGeneration = (queueDeviceGenerationRef.current.get(deviceId) || 0) + 1;
    queueDeviceGenerationRef.current.set(deviceId, deviceGeneration);
    const requestId = updateState ? queueRequestIdRef.current + 1 : null;
    if (updateState) {
      queueRequestIdRef.current = requestId;
      if (isForeground) {
        queueForegroundRequestIdRef.current = requestId;
        setQueueLoading(true);
        setQueueError('');
      }
    }
    try {
      const data = await queueApi.getByDevice(deviceId);
      const sortedQueue = (data.queue || [])
        .slice()
        .sort((a, b) => a.position - b.position);
      queueSnapshotsByDeviceRef.current.set(deviceId, sortedQueue);
      const sortedLogs = (data.logs || []).slice().sort((a, b) => new Date(b.change_time) - new Date(a.change_time));
      const canUpdateState = updateState
        && queueRequestIdRef.current === requestId
        && selectedDeviceIdRef.current === deviceId;
      if (canUpdateState) {
        setQueue(sortedQueue);
        setQueueLogs(sortedLogs);
        setQueueError('');
        setDevices(prev => prev.map(device => (
          device.id === deviceId ? { ...device, queue_count: sortedQueue.length } : device
        )));
      }
      if (queueDeviceGenerationRef.current.get(deviceId) !== deviceGeneration) return;
      syncQueueNoticeEntries(sortedQueue, deviceId);
      const activeEntry = getActiveQueueEntry(sortedQueue);
      const activeNoticePending = Boolean(activeEntry && getQueueNoticeEntries().some(entry => (
        String(entry.id) === String(activeEntry.id)
        && String(entry.device_id) === String(deviceId)
      )));
      const notifyIntent = queueNotifyIntentRef.current.get(deviceId);
      if (notifyIntent?.notify || activeNoticePending) {
        await notifyActiveQueueEntry(
          sortedQueue,
          deviceId,
          notifyIntent?.reason || reason,
          notifyIntent?.notificationContext || notificationContext
        );
        if (queueNotifyIntentRef.current.get(deviceId) === notifyIntent) {
          queueNotifyIntentRef.current.delete(deviceId);
        }
      } else {
        syncActiveQueueEntry(deviceId, sortedQueue);
      }
    } catch (error) {
      if (
        updateState
        && queueRequestIdRef.current === requestId
        && selectedDeviceIdRef.current === deviceId
      ) {
        if (!silent) {
          setQueue([]);
          setQueueLogs([]);
          setQueueError(error?.message || '排队列表加载失败');
        }
      }
    } finally {
      if (isForeground && queueForegroundRequestIdRef.current === requestId) {
        queueForegroundRequestIdRef.current = null;
      }
      if (updateState && queueRequestIdRef.current === requestId) {
        setQueueLoading(false);
      }
      if (isForeground && mountedRef.current) {
        const pending = pendingQueueRefreshRef.current.get(deviceId);
        if (pending) {
          pendingQueueRefreshRef.current.delete(deviceId);
          fetchQueue(deviceId, {
            ...pending,
            silent: true,
            updateState: selectedDeviceIdRef.current === deviceId,
          });
        }
      }
    }
  };

  const fetchRecentResults = async (device, options = {}) => {
    const { silent = false } = options;
    if (!device?.id) {
      resultsRequestIdRef.current += 1;
      resultsForegroundRequestIdRef.current = null;
      resultsLoadedDeviceIdRef.current = null;
      setRecentResults([]);
      setResultsLoading(false);
      setResultsAvailability('ready');
      setResultsError('');
      return;
    }
    if (device.status === 'offline') {
      resultsRequestIdRef.current += 1;
      resultsForegroundRequestIdRef.current = null;
      resultsLoadedDeviceIdRef.current = device.id;
      setRecentResults([]);
      setResultsLoading(false);
      setResultsAvailability('offline');
      setResultsError('');
      return;
    }
    if (!device.client_base_url) {
      resultsRequestIdRef.current += 1;
      resultsForegroundRequestIdRef.current = null;
      resultsLoadedDeviceIdRef.current = device.id;
      setRecentResults([]);
      setResultsLoading(false);
      setResultsAvailability('unconfigured');
      setResultsError('');
      return;
    }
    if (silent && resultsForegroundRequestIdRef.current != null) return;
    const requestId = resultsRequestIdRef.current + 1;
    resultsRequestIdRef.current = requestId;
    if (!silent) {
      resultsForegroundRequestIdRef.current = requestId;
      setResultsLoading(true);
      setResultsError('');
      setResultsAvailability('ready');
    }
    try {
      const data = await resultsApi.getRecent(device.id, 5);
      if (
        resultsRequestIdRef.current !== requestId
        || selectedDeviceIdRef.current !== device.id
      ) return;
      resultsLoadedDeviceIdRef.current = device.id;
      setRecentResults(Array.isArray(data?.items) ? data.items : []);
      setResultsError('');
      setResultsAvailability('ready');
    } catch (error) {
      if (
        resultsRequestIdRef.current !== requestId
        || selectedDeviceIdRef.current !== device.id
      ) return;
      if (!silent) {
        resultsLoadedDeviceIdRef.current = device.id;
        setRecentResults([]);
        setResultsError(error?.status === 502
          ? '设备结果服务暂时不可达，请检查客户端结果服务和网络'
          : error?.message || '最近结果加载失败');
      }
    } finally {
      if (!silent && resultsForegroundRequestIdRef.current === requestId) {
        resultsForegroundRequestIdRef.current = null;
      }
      if (resultsRequestIdRef.current === requestId) setResultsLoading(false);
    }
  };

  const requestNotificationPermission = async () => {
    try {
      if (!('Notification' in window)) {
        message.warning('当前浏览器不支持系统通知');
        return false;
      }
      if (Notification.permission === 'granted') {
        return true;
      }
      if (Notification.permission === 'denied') {
        message.warning('系统通知已被禁用，请在浏览器设置中开启');
        return false;
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        message.warning('未获得系统通知权限');
        return false;
      }
      return true;
    } catch (error) {
      message.warning('系统通知权限申请失败，但不会影响排队操作');
      return false;
    }
  };

  const showPersistentNotice = (title, content, options = {}) => {
    if (!mountedRef.current) return;
    let acknowledged = false;
    return modal.confirm({
      title,
      content,
      okText: '我知道了',
      cancelButtonProps: { style: { display: 'none' } },
      maskClosable: false,
      closable: false,
      keyboard: false,
      centered: true,
      onOk: () => {
        acknowledged = true;
        options.onAcknowledge?.();
      },
      afterClose: () => options.afterClose?.({ acknowledged }),
    });
  };

  const sendQueueNotification = (entry, deviceName) => {
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      return;
    }
    const title = '排队提醒';
    const body = `${deviceName || '设备'} - ${entry.inspector_name || '检验员'} 已轮到`;
    new Notification(title, { body });
  };

  const sendDeviceNotification = (device) => {
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      return;
    }
    const title = '检测完成提醒';
    const body = `${device.name || '设备'} 检测完成`;
    new Notification(title, { body });
  };

  const sendCustomNotification = (title, body) => {
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      return;
    }
    new Notification(title, { body });
  };

  const getNotifyModeByDevice = (deviceId) => {
    if (deviceId == null) return 'off';
    const key = String(deviceId);
    return notifyModesRef.current[key] || 'off';
  };

  const claimCompletionNotice = async (source = {}) => {
    const deviceId = source.device_id ?? source.deviceId ?? source.id;
    if (deviceId == null) return false;
    const device = devicesRef.current.find(item => String(item.id) === String(deviceId));
    const claim = buildCompletionNoticeClaim({
      ...source,
      device_id: deviceId,
      task_id: source.task_id ?? source.taskId ?? device?.task_id,
      task_key: source.task_key ?? source.taskKey ?? device?.task_key,
      task_name: source.task_name ?? source.taskName ?? device?.task_name,
    });
    return claimNotificationOnce(claim);
  };

  const syncActiveQueueEntry = (deviceId, queueList) => {
    if (deviceId == null) return;
    const activeEntry = getActiveQueueEntry(queueList);
    activeQueueRef.current.set(deviceId, activeEntry ? activeEntry.id : null);
  };

  const syncQueueNoticeEntries = (queueList, deviceId) => {
    if (deviceId == null || !Array.isArray(queueList)) return;
    const userId = queueUserIdRef.current;
    if (!userId) return;
    queueList.forEach(record => {
      if (record?.position !== 1 && record?.created_by_id && record.created_by_id === userId) {
        addQueueNoticeEntry({
          id: record.id,
          device_id: deviceId,
          inspector_name: record.inspector_name,
          created_by_id: userId,
        });
      }
    });
  };

  const notifyActiveQueueEntry = async (queueList, deviceId, reason, notificationContext = {}) => {
    if (deviceId == null) return;
    const activeEntry = getActiveQueueEntry(queueList);
    const activeId = activeEntry ? activeEntry.id : null;
    const previousId = activeQueueRef.current.get(deviceId);
    activeQueueRef.current.set(deviceId, activeId);

    if (!activeEntry || activeId == null) {
      return;
    }

    const userId = queueUserIdRef.current;
    if (!userId || activeEntry.created_by_id !== userId) {
      return;
    }

    const hasPendingNotice = getQueueNoticeEntries().some(entry => (
      String(entry.id) === String(activeId)
      && String(entry.device_id) === String(deviceId)
    ));
    if ((activeId === previousId && !hasPendingNotice) || queueNoticeModalIdsRef.current.has(activeId)) {
      return;
    }

    // “轮到您”是排队后的强制提醒，不能被同一完成边沿上的可选
    // “检测完成”提醒占用去重窗口。它按稳定队列记录单独去重。
    const claimed = await claimNotificationOnce(buildQueueTurnNoticeClaim(deviceId, activeId));
    if (!mountedRef.current || !claimed) {
      return;
    }

    if (reason === 'complete') {
      const completionTimer = queueCompletionTimersRef.current.get(deviceId);
      if (completionTimer) {
        window.clearTimeout(completionTimer);
        queueCompletionTimersRef.current.delete(deviceId);
      }
      const deviceTimer = deviceNotifyTimersRef.current.get(deviceId);
      if (deviceTimer) {
        window.clearTimeout(deviceTimer);
        deviceNotifyTimersRef.current.delete(deviceId);
      }
      if (getNotifyModeByDevice(deviceId) === 'once') {
        setNotifyModes(prev => ({ ...prev, [String(deviceId)]: 'off' }));
      }
      // 先取得“轮到您”提醒，再占用完成提醒的短租约，避免同一
      // 完成边沿随后再弹出一条含义较弱的完成/移除通知。
      await claimCompletionNotice({
        ...notificationContext,
        device_id: deviceId,
      });
    }
    queueNoticeModalIdsRef.current.add(activeId);

    const deviceName = devicesRef.current.find(device => device.id === deviceId)?.name || '';
    const inspectorName = activeEntry.inspector_name || '检验员';
    showPersistentNotice(
      '排队提醒',
      `${deviceName || '设备'} - ${inspectorName} 已轮到`,
      {
        onAcknowledge: () => removeQueueNoticeEntry(activeId),
        afterClose: () => queueNoticeModalIdsRef.current.delete(activeId),
      }
    );

    const permitted = await requestNotificationPermission();
    if (!mountedRef.current || !permitted) {
      return;
    }
    sendQueueNotification(activeEntry, deviceName);

  };

  const scheduleDeviceNotification = (device) => {
    if (!device?.id) return;
    const mode = getNotifyModeByDevice(device.id);
    if (mode === 'off') return;
    const timers = deviceNotifyTimersRef.current;
    if (timers.has(device.id)) {
      clearTimeout(timers.get(device.id));
    }
    const timerId = setTimeout(async () => {
      timers.delete(device.id);
      if (!mountedRef.current) return;
      const claimed = await claimCompletionNotice(device);
      if (!mountedRef.current || !claimed) return;
      if (mode === 'once') {
        setNotifyModes(prev => ({
          ...prev,
          [String(device.id)]: 'off'
        }));
      }
      showPersistentNotice(
        '检测完成提醒',
        `${device.name || '设备'} 检测完成`
      );
      const permitted = await requestNotificationPermission();
      if (!mountedRef.current || !permitted) {
        return;
      }
      sendDeviceNotification(device);
    }, 1200);
    timers.set(device.id, timerId);
  };

  const handleExtendTimeout = async (deviceId) => {
    if (!deviceId) return;
    const changedBy = inspectorName?.trim() || '系统';
    setExtendingDeviceId(deviceId);
    try {
      const response = await queueApi.extendTimeout(deviceId, {
        changed_by: changedBy,
        changed_by_id: queueUserIdRef.current,
      });
      const deadlineAt = response?.data?.queue_timeout_deadline_at;
      const extendedCount = response?.data?.queue_timeout_extended_count;
      const activeId = response?.data?.queue_timeout_active_id;
      if (deadlineAt) {
        setDevices(prev => prev.map(device =>
          device.id === deviceId
            ? {
              ...device,
              queue_timeout_deadline_at: deadlineAt,
              queue_timeout_extended_count: extendedCount ?? device.queue_timeout_extended_count,
              queue_timeout_active_id: activeId ?? device.queue_timeout_active_id,
            }
            : device
        ));
      }
      message.success('已延长5分钟');
      if (selectedDeviceIdRef.current === deviceId) {
        fetchQueue(deviceId, { silent: true });
      }
    } catch (error) {
      message.error(error.message || '延长超时失败');
    } finally {
      setExtendingDeviceId(null);
    }
  };

  const notifyQueueTimeoutReminder = async (payload) => {
    if (!payload) return;
    const userId = queueUserIdRef.current;
    if (!userId) return;
    const deviceName = payload.device_name || '设备';

    if (payload.active_created_by_id && payload.active_created_by_id === userId) {
      const content = `${deviceName} 已空闲超过1分钟，请尽快开始使用`;
      showPersistentNotice('排队提醒', content);
      const permitted = await requestNotificationPermission();
      if (mountedRef.current && permitted) {
        sendCustomNotification('排队提醒', content);
      }
    }

    if (payload.next_created_by_id && payload.next_created_by_id === userId) {
      const content = `${deviceName} 当前使用人未开始，请注意顺位变化`;
      showPersistentNotice('排队提醒', content);
      const permitted = await requestNotificationPermission();
      if (mountedRef.current && permitted) {
        sendCustomNotification('排队提醒', content);
      }
    }
  };

  const notifyQueueTimeoutShift = async (payload) => {
    if (!payload) return;
    const userId = queueUserIdRef.current;
    if (!userId) return;
    if (payload.timed_out_created_by_id && payload.timed_out_created_by_id === userId) {
      const deviceName = payload.device_name || '设备';
      const content = `${deviceName} 超时未开始使用，系统已将你与下一位互换顺序`;
      showPersistentNotice('排队提醒', content);
      const permitted = await requestNotificationPermission();
      if (mountedRef.current && permitted) {
        sendCustomNotification('排队提醒', content);
      }
    }
  };

  const notifyQueueCompletion = (payload) => {
    if (!payload) return;
    const userId = queueUserIdRef.current;
    if (!userId) return;
    if (payload.completed_by_id && payload.completed_by_id === userId) {
      const deviceId = payload.device_id;
      if (deviceId == null) return;
      const previousQueue = queueSnapshotsByDeviceRef.current.get(deviceId) || [];
      const completedIndex = previousQueue.findIndex(record => (
        queueRecordIdEquals(record.id, payload.queue_id)
      ));
      const nextRecord = completedIndex >= 0 ? previousQueue[completedIndex + 1] : null;
      if (nextRecord?.created_by_id === userId) {
        // 同一浏览器连续排了多份时，下一份的“轮到您”提醒信息量更高。
        // 预占完成提醒租约，等待随后刷新队列触发强制轮到提醒。
        void claimCompletionNotice(payload);
        return;
      }
      const existingTimer = queueCompletionTimersRef.current.get(deviceId);
      if (existingTimer) window.clearTimeout(existingTimer);
      const timerId = window.setTimeout(async () => {
        queueCompletionTimersRef.current.delete(deviceId);
        if (!mountedRef.current) return;
        const claimed = await claimCompletionNotice(payload);
        if (!mountedRef.current || !claimed) return;
        const deviceName = payload.device_name || '设备';
        const inspectorName = payload.completed_by || '检验员';
        const content = `${deviceName} 检测完成，${inspectorName} 已从排队移除`;
        showPersistentNotice('检测完成提醒', content);
        const permitted = await requestNotificationPermission();
        if (mountedRef.current && permitted) {
          sendCustomNotification('检测完成提醒', content);
        }
      }, 900);
      queueCompletionTimersRef.current.set(deviceId, timerId);
    }
  };

  useEffect(() => {
    const progressMap = lastProgressRef.current;
    devices.forEach(device => {
      const prev = progressMap.get(device.id);
      const next = device.task_progress;
      if (prev !== undefined && prev !== 100 && next === 100) {
        scheduleDeviceNotification(device);
      }
      progressMap.set(device.id, next);
    });
  }, [devices]);

  useEffect(() => {
    fetchDevices();

    const bumpDeviceLiveRevision = (deviceId) => {
      if (deviceId == null) return;
      const current = devicesLiveRevisionRef.current.get(deviceId) || 0;
      devicesLiveRevisionRef.current.set(deviceId, current + 1);
    };

    wsClient.on('device_status_update', (data) => {
      if (!data || data.device_id == null) return;
      bumpDeviceLiveRevision(data.device_id);
      setLastUpdatedAt(new Date());
      setDevices(prev => prev.map(device => 
        device.id === data.device_id 
          ? { ...device, ...data, offline_last_seen: data.status === 'offline' ? device.offline_last_seen : null }
          : device
      ));
    });

    wsClient.on('queue_timeout_update', (data) => {
      if (!data || data.device_id == null) return;
      bumpDeviceLiveRevision(data.device_id);
      const { device_id: deviceId, ...timeoutData } = data;
      setDevices(prev => prev.map(device =>
        device.id === deviceId
          ? { ...device, ...timeoutData }
          : device
      ));
    });

    wsClient.on('queue_timeout_reminder', (data) => {
      notifyQueueTimeoutReminder(data);
    });

    wsClient.on('queue_timeout_shift', (data) => {
      notifyQueueTimeoutShift(data);
    });

    const sortDevices = (items) => {
      return items
        .slice()
        .sort((a, b) => {
          const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
          const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
          if (aTime !== bTime) {
            return aTime - bTime;
          }
          return a.id - b.id;
        });
    };

    wsClient.on('device_list_update', (data) => {
      if (!data) return;
      bumpDeviceLiveRevision(data.device_id ?? data.device?.id);
      if (data.action === 'delete') {
        setDevices(prev => prev.filter(device => device.id !== data.device_id));
        if (selectedDeviceIdRef.current === data.device_id) {
          selectedDeviceIdRef.current = null;
          setSelectedDeviceId(null);
        }
        return;
      }
      if (data.device) {
        setDevices(prev => {
          const exists = prev.find(device => device.id === data.device.id);
          if (exists) {
            return sortDevices(prev.map(device => device.id === data.device.id ? { ...device, ...data.device } : device));
          }
          return sortDevices([...prev, data.device]);
        });
      }
    });

    wsClient.on('device_offline', (data) => {
      if (!data || data.device_id == null) return;
      bumpDeviceLiveRevision(data.device_id);
      setDevices(prev => prev.map(device => 
        device.id === data.device_id 
          ? { ...device, status: 'offline', offline_last_seen: data.last_seen || device.last_heartbeat }
          : device
      ));
    });

    wsClient.on('queue_update', (data) => {
      if (!data || data.device_id == null) return;
      bumpDeviceLiveRevision(data.device_id);
      if (data.action === 'complete') {
        notifyQueueCompletion(data);
      }
      if (['complete', 'leave', 'placeholder_delete'].includes(data.action) && data.queue_id) {
        removeQueueNoticeEntry(data.queue_id);
      }
      const currentSelectedId = selectedDeviceIdRef.current;
      if (data.device_id === currentSelectedId) {
        fetchQueue(currentSelectedId, {
          notify: true,
          reason: data.action,
          notificationContext: data,
          silent: true,
        });
      } else {
        fetchQueue(data.device_id, {
          notify: true,
          reason: data.action,
          notificationContext: data,
          updateState: false,
        });
      }
      setDevices(prev => prev.map(device =>
        device.id === data.device_id
          ? { ...device, queue_count: data.queue_count ?? device.queue_count }
          : device
      ));
    });

    const pollId = setInterval(() => {
      fetchDevices();
      const currentSelectedId = selectedDeviceIdRef.current;
      if (currentSelectedId) {
        fetchQueue(currentSelectedId, { silent: true });
      }
    }, 8000);

    return () => {
      wsClient.off('device_status_update');
      wsClient.off('device_list_update');
      wsClient.off('device_offline');
      wsClient.off('queue_update');
      wsClient.off('queue_timeout_update');
      wsClient.off('queue_timeout_reminder');
      wsClient.off('queue_timeout_shift');
      clearInterval(pollId);
    };
  }, []);

  useEffect(() => {
    destroyManagedModals();
    queueRequestIdRef.current += 1;
    resultsRequestIdRef.current += 1;
    queueForegroundRequestIdRef.current = null;
    resultsForegroundRequestIdRef.current = null;
    resultsLoadedDeviceIdRef.current = null;
    setQueue([]);
    setQueueLogs([]);
    setQueueError('');
    setQueueLoading(false);
    setRecentResults([]);
    setResultsError('');
    setResultsLoading(false);
    setResultsAvailability('ready');
    if (selectedDeviceId) {
      fetchQueue(selectedDeviceId);
    }
    setClaimModal({ open: false, record: null, submittingAction: null });
    claimForm.resetFields();
    setTableModal({ open: false, folder: null });
    setImagesModal({ open: false, folder: null });
    setCleanupModal(prev => ({ ...prev, open: false, submitting: false }));
  }, [claimForm, destroyManagedModals, selectedDeviceId]);

  useEffect(() => {
    if (!selectedDeviceId || !selectedDevice) return;
    fetchRecentResults(selectedDevice, {
      silent: resultsLoadedDeviceIdRef.current === selectedDevice.id,
    });
  }, [
    selectedDeviceId,
    selectedDevice?.task_name,
    selectedDevice?.task_progress,
    selectedDevice?.status,
    selectedDevice?.client_base_url,
  ]);

  useEffect(() => {
    if (!selectedDeviceId) return;
    const pollId = setInterval(() => {
      const device = devicesRef.current.find(item => item.id === selectedDeviceId);
      fetchRecentResults(device, { silent: true });
    }, 60000);
    return () => clearInterval(pollId);
  }, [selectedDeviceId]);

  const handleSelectDevice = (deviceId) => {
    if (deviceId !== selectedDeviceIdRef.current) {
      selectedDeviceIdRef.current = deviceId;
      setSelectedDeviceId(deviceId);
    }
  };

  const handleQuickQueue = () => {
    const anchor = queueFormAnchorRef.current;
    if (!anchor) return;
    if (queueFocusTimerRef.current) {
      window.clearTimeout(queueFocusTimerRef.current);
    }
    setQueueFocusPulse(true);
    anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
    window.requestAnimationFrame(() => {
      form.getFieldInstance('inspector_name')?.focus?.({ preventScroll: true });
    });
    queueFocusTimerRef.current = window.setTimeout(() => {
      setQueueFocusPulse(false);
      queueFocusTimerRef.current = null;
    }, 1600);
  };

  const handleRefreshAll = async () => {
    await fetchDevices();
    const currentDeviceId = selectedDeviceIdRef.current;
    const currentDevice = devicesRef.current.find(item => item.id === currentDeviceId);
    await Promise.all([
      currentDeviceId ? fetchQueue(currentDeviceId) : Promise.resolve(),
      currentDevice ? fetchRecentResults(currentDevice) : Promise.resolve(),
    ]);
  };

  const buildResultsUrl = (type, folder) => {
    if (!selectedDevice?.id) return '';
    const basePath = type === 'table' ? '/results/table' : '/results/images';
    const folderParam = folder ? `&folder=${encodeURIComponent(folder)}` : '';
    return `${basePath}?device_id=${selectedDevice.id}${folderParam}`;
  };

  const openCleanupModal = (folder) => {
    if (!selectedDevice?.id) return;
    const fallbackFolder = getValidOutputPath(selectedDevice?.metrics?.olympus?.output_path) || '';
    const sourceFolder = folder || fallbackFolder;
    const sourceFolderName = getFolderNameFromPath(sourceFolder);
    setCleanupModal({
      open: true,
      folder: folder || null,
      sourceFolderName,
      renameEnabled: false,
      newFolderName: getDefaultRenameName(sourceFolderName),
      submitting: false,
    });
  };

  const closeCleanupModal = () => {
    setCleanupModal(prev => {
      if (prev.submitting) return prev;
      return { ...prev, open: false };
    });
  };

  const handleCleanupSubmit = async () => {
    if (!selectedDevice?.id) return;

    const renameName = cleanupModal.newFolderName.trim();
    if (cleanupModal.renameEnabled) {
      if (!renameName) {
        message.error('新文件夹名称不能为空');
        return;
      }
      if (renameName === '.' || renameName === '..' || invalidFolderNamePattern.test(renameName)) {
        message.error('新文件夹名称不合法，不能包含 \\ / : * ? " < > |');
        return;
      }
    }

    setCleanupModal(prev => ({ ...prev, submitting: true }));
    try {
      const data = await resultsApi.cleanupImages(
        selectedDevice.id,
        cleanupModal.folder || undefined,
        {
          renameEnabled: cleanupModal.renameEnabled,
          newFolderName: cleanupModal.renameEnabled ? renameName : undefined,
        }
      );
      const moved = data?.moved ?? 0;
      if (cleanupModal.renameEnabled && data?.renamed) {
        message.success(`已移动 ${moved} 张图片，文件夹已重命名为 ${data?.new_folder || renameName}`);
      } else {
        message.success(`已移动 ${moved} 张图片`);
      }
      setCleanupModal(prev => ({ ...prev, open: false, submitting: false }));
      if (data?.renamed) {
        setImagesModal({ open: false, folder: null });
      }
      fetchRecentResults(selectedDevice, { silent: true });
    } catch (error) {
      setCleanupModal(prev => ({ ...prev, submitting: false }));
      const msg = error?.message || '';
      if (msg.includes('folder_not_found')) {
        message.error('输出路径不存在，无法清理');
      } else if (msg.includes('output_parent_missing')) {
        message.error('输出路径缺少父级目录，无法清理');
      } else if (msg.includes('cleanup_not_supported')) {
        message.error('当前设备不支持清理');
      } else if (msg.includes('rename_name_empty')) {
        message.error('新文件夹名称不能为空');
      } else if (msg.includes('rename_invalid_name')) {
        message.error('新文件夹名称不合法');
      } else if (msg.includes('rename_target_exists')) {
        message.error('目标文件夹已存在，请更换名称');
      } else if (msg.includes('rename_failed')) {
        message.error('重命名文件夹失败');
      } else {
        message.error('删图/重命名文件夹失败');
      }
    }
  };

  const openClaimModal = (record) => {
    const rememberedName = (inspectorName || getInspectorName() || '').trim();
    claimForm.setFieldsValue({ inspector_name: rememberedName });
    setClaimModal({
      open: true,
      record,
      submittingAction: null,
    });
  };

  const closeClaimModal = () => {
    setClaimModal(prev => {
      if (prev.submittingAction) return prev;
      return {
        open: false,
        record: null,
        submittingAction: null,
      };
    });
    claimForm.resetFields();
  };

  const handleClaimPlaceholder = async () => {
    if (!claimModal.record) return;

    try {
      const values = await claimForm.validateFields();
      const nextInspectorName = values.inspector_name.trim();
      setClaimModal(prev => ({ ...prev, submittingAction: 'claim' }));
      await queueApi.claim(claimModal.record.id, {
        inspector_name: nextInspectorName,
        claimed_by_id: queueUserIdRef.current,
      });
      saveInspectorName(nextInspectorName);
      setInspectorName(nextInspectorName);
      form.setFieldsValue({ inspector_name: nextInspectorName });
      message.success('认领占位成功');
      setClaimModal({
        open: false,
        record: null,
        submittingAction: null,
      });
      claimForm.resetFields();
      fetchQueue(selectedDeviceId, { notify: true, reason: 'placeholder_claim', silent: true });
    } catch (error) {
      if (error?.errorFields) {
        return;
      }
      setClaimModal(prev => ({ ...prev, submittingAction: null }));
      message.error(error?.message || '认领占位失败');
    }
  };

  const handleDeletePlaceholder = async () => {
    if (!claimModal.record) return;

    try {
      setClaimModal(prev => ({ ...prev, submittingAction: 'delete' }));
      await queueApi.leave(claimModal.record.id, { changed_by_id: queueUserIdRef.current });
      removeQueueNoticeEntry(claimModal.record.id);
      message.success('占位人员已删除');
      setClaimModal({
        open: false,
        record: null,
        submittingAction: null,
      });
      claimForm.resetFields();
      fetchQueue(selectedDeviceId, { notify: true, reason: 'placeholder_delete', silent: true });
    } catch (error) {
      setClaimModal(prev => ({ ...prev, submittingAction: null }));
      message.error(error?.message || '删除占位人员失败');
    }
  };

  const handleJoinQueue = async (values) => {
    setQueueSubmitting(true);
    try {
      const nextInspectorName = values.inspector_name.trim();
      const records = await queueApi.join({
        inspector_name: nextInspectorName,
        device_id: selectedDeviceId,
        copies: values.copies || 1,
        created_by_id: queueUserIdRef.current,
      });

      const requestedCopies = values.copies || 1;
      const actualCopies = Array.isArray(records) ? records.length : 0;

      if (records && records.length > 0) {
        for (let i = 0; i < records.length; i++) {
          addQueueNoticeEntry({
            id: records[i].id,
            device_id: selectedDeviceId,
            inspector_name: nextInspectorName,
            created_by_id: queueUserIdRef.current,
          });
        }
      }

      if (actualCopies > 0) {
        message.success(`已加入 ${actualCopies} 份，轮到您时会自动提醒`);
      }
      if (actualCopies > 0 && actualCopies < requestedCopies) {
        message.info(`已按配额加入 ${actualCopies} 份（超出部分忽略）`);
      }
      saveInspectorName(nextInspectorName);
      setInspectorName(nextInspectorName);
      form.setFieldsValue({
        inspector_name: nextInspectorName,
        copies: 1,
      });
      fetchQueue(selectedDeviceId, { notify: true, reason: 'join', silent: true });

      // 排队成功是主流程，通知授权只能作为成功后的附加动作；浏览器
      // 拒绝、阻止或延迟权限弹窗都不能回滚或伪装成排队失败。
      try {
        if ('Notification' in window && Notification.permission === 'default') {
          void requestNotificationPermission();
        }
      } catch (notificationError) {
        message.warning('已成功加入排队，但浏览器未能申请系统通知权限');
      }
    } catch (error) {
      message.error(error?.message || '加入排队失败');
    } finally {
      setQueueSubmitting(false);
    }
  };

  const handleChangePosition = (record, newPosition, dropContext = null) => {
    const deviceId = selectedDeviceIdRef.current;
    const fromLabel = getQueuePositionLabel(record.position);
    const toLabel = getQueuePositionLabel(newPosition);
    openManagedConfirm({
      title: '确认移动位置',
      content: `确定要将 ${record.inspector_name} 从 ${fromLabel} 移动到 ${toLabel} 吗？`,
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        try {
          let recordToMove = record;
          let targetPosition = newPosition;
          let targetRecord = null;
          if (dropContext) {
            const latestData = await queueApi.getByDevice(deviceId);
            const latestQueue = (latestData?.queue || [])
              .slice()
              .sort((a, b) => a.position - b.position);
            const latestDrop = resolveStableQueueDrop(
              latestQueue,
              dropContext.dragDescriptor,
              dropContext.targetDescriptor
            );
            if (!latestDrop.ok) {
              message.warning('排队顺序已发生变化，本次拖动已取消，请重新拖动');
              fetchQueue(deviceId, { silent: true });
              return;
            }
            recordToMove = latestDrop.dragRecord;
            targetRecord = latestDrop.targetRecord;
            targetPosition = latestDrop.newPosition;
          }

          const changedBy = inspectorName?.trim() || '系统';
          await queueApi.updatePosition(recordToMove.id, {
            new_position: targetPosition,
            changed_by: changedBy,
            version: recordToMove.version,
            changed_by_id: queueUserIdRef.current,
            ...(targetRecord ? {
              target_queue_id: targetRecord.id,
              target_version: targetRecord.version,
            } : {}),
          });
          message.success('修改位置成功');
          fetchQueue(deviceId, { notify: true, reason: 'position_change', silent: true });
        } catch (error) {
          if (error?.status === 409) {
            message.error(error?.body?.message || '队列已被其他用户修改，请重新拖动');
            const conflictQueue = error?.body?.queue;
            if (
              Array.isArray(conflictQueue)
              && selectedDeviceIdRef.current === deviceId
            ) {
              const sortedConflictQueue = conflictQueue
                .slice()
                .sort((a, b) => a.position - b.position);
              queueSnapshotsByDeviceRef.current.set(deviceId, sortedConflictQueue);
              queueRef.current = sortedConflictQueue;
              setQueue(sortedConflictQueue);
              setDevices(prev => prev.map(device => (
                device.id === deviceId
                  ? { ...device, queue_count: sortedConflictQueue.length }
                  : device
              )));
            }
            fetchQueue(deviceId, { silent: true });
          } else {
            message.error(error?.detail?.message || error?.message || '修改位置失败');
          }
        }
      }
    });
  };

  const handleDeleteQueue = async (record) => {
    const deviceId = selectedDeviceIdRef.current;
    openManagedConfirm({
      title: '确认删除',
      content: `确定要将 ${record.inspector_name} 从排队中移除吗？`,
      okText: '确认',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await queueApi.leave(record.id, { changed_by_id: queueUserIdRef.current });
          message.success('离开排队成功');
          removeQueueNoticeEntry(record.id);
          fetchQueue(deviceId, { notify: true, reason: 'leave', silent: true });
        } catch (error) {
          message.error('离开排队失败');
        }
      }
    });
  };

  const notifyMode = selectedDeviceId != null
    ? (notifyModes[String(selectedDeviceId)] || 'off')
    : 'off';

  const handleNotifyModeChange = async (nextMode) => {
    if (selectedDeviceId == null) return false;
    if (nextMode !== 'off') {
      const permitted = await requestNotificationPermission();
      if (!permitted) {
        setNotifyModes(prev => ({
          ...prev,
          [String(selectedDeviceId)]: 'off'
        }));
        return false;
      }
    }
    setNotifyModes(prev => ({
      ...prev,
      [String(selectedDeviceId)]: nextMode
    }));
    return true;
  };

  const handleNotifyModeSelect = async ({ key }) => {
    const nextMode = key;
    if (!['off', 'once', 'always'].includes(nextMode) || nextMode === notifyMode) return;
    const applied = await handleNotifyModeChange(nextMode);
    if (!applied) return;
    if (nextMode === 'off') {
      message.info('完成提醒已关闭');
    } else if (nextMode === 'once') {
      message.success('完成提醒已开启：下次检测完成时提醒一次');
    } else {
      message.success('完成提醒已开启：本设备每次检测完成都会提醒');
    }
  };

  const notifyModeLabel = notifyMode === 'once'
    ? '提醒一次'
    : notifyMode === 'always'
      ? '持续提醒'
      : '关闭';

  const completionNotifyMenuItems = [
    {
      key: 'off',
      icon: <ClockCircleOutlined />,
      label: (
        <span className="monitor-completion-notify-option">
          <strong>关闭</strong>
          <small>不监听本设备检测完成</small>
        </span>
      ),
    },
    {
      key: 'once',
      icon: <ClockCircleOutlined />,
      label: (
        <span className="monitor-completion-notify-option">
          <strong>提醒一次</strong>
          <small>下次检测完成后提醒并自动关闭</small>
        </span>
      ),
    },
    {
      key: 'always',
      icon: <ClockCircleFilled />,
      label: (
        <span className="monitor-completion-notify-option">
          <strong>持续提醒</strong>
          <small>本设备每次检测完成都提醒</small>
        </span>
      ),
    },
  ];

  const handleDropConfirm = (dragDescriptor, targetDescriptor) => {
    const resolvedDrop = resolveStableQueueDrop(
      queueRef.current,
      dragDescriptor,
      targetDescriptor
    );
    if (!resolvedDrop.ok) {
      if (resolvedDrop.reason !== 'same_record') {
        message.warning('排队顺序在拖动期间已更新，请重新拖动');
        const deviceId = selectedDeviceIdRef.current;
        if (deviceId) fetchQueue(deviceId, { silent: true });
      }
      return;
    }

    handleChangePosition(resolvedDrop.dragRecord, resolvedDrop.newPosition, {
      dragDescriptor,
      targetDescriptor,
    });
  };

  const queueColumns = [
    {
      title: '',
      dataIndex: 'drag',
      key: 'drag',
      width: 42,
      render: () => (
        <Tooltip title="按住任意非按钮区域拖动整行">
          <HolderOutlined className="monitor-queue-drag-indicator" aria-hidden />
        </Tooltip>
      ),
    },
    {
      title: '位置',
      dataIndex: 'position',
      key: 'position',
      width: 100,
      render: (_, record) => (
        record.position === 1
          ? <span style={{ color: '#389e0d', fontWeight: 600 }}>正在使用</span>
          : getQueuePositionDisplay(record.position)
      )
    },
    {
      title: '检验员',
      dataIndex: 'inspector_name',
      key: 'inspector_name',
      render: (_, record) => {
        const isMine = record.created_by_id && record.created_by_id === queueUserIdRef.current;
        return (
          <span className="monitor-queue-person">
            <span>{record.inspector_name}</span>
            {isUnclaimedPlaceholder(record) ? <Tag color="gold">占位</Tag> : null}
            {isMine ? <Tag color="blue">本人</Tag> : null}
          </span>
        );
      },
    },
    { title: '加入时间', dataIndex: 'submitted_at', key: 'submitted_at', width: 92, render: formatTime },
    {
      title: '操作',
      key: 'actions',
      width: 80,
      render: (_, record) => (
        isUnclaimedPlaceholder(record) ? (
          <Button
            type="link"
            size="small"
            onMouseDown={event => event.stopPropagation()}
            onClick={() => openClaimModal(record)}
          >
            认领
          </Button>
        ) : (
          <Tooltip title="移出排队">
            <Button
              type="text"
              size="small"
              danger
              aria-label="删除排队记录"
              icon={<DeleteOutlined />}
              onMouseDown={event => event.stopPropagation()}
              onClick={() => handleDeleteQueue(record)}
            />
          </Tooltip>
        )
      ),
    },
  ];

  const filteredDevices = useMemo(() => {
    const keyword = searchText.trim().toLocaleLowerCase();
    const statusPriority = { error: 0, offline: 1, maintenance: 2, busy: 3, idle: 4 };
    const filtered = devices.filter(device => {
      const matchesKeyword = !keyword || [device.name, device.device_code, device.model, device.location]
        .filter(Boolean)
        .some(value => String(value).toLocaleLowerCase().includes(keyword));
      const matchesStatus = statusFilter === 'all'
        || (statusFilter === 'attention' && ['maintenance', 'error', 'offline'].includes(device.status))
        || device.status === statusFilter;
      const confocal = isConfocalDevice(device);
      const matchesType = deviceTypeFilter === 'all'
        || (deviceTypeFilter === 'confocal' && confocal)
        || (deviceTypeFilter === 'standard' && !confocal);
      return matchesKeyword && matchesStatus && matchesType;
    });

    return filtered.slice().sort((left, right) => {
      if (sortMode === 'attention') {
        return (statusPriority[left.status] ?? 9) - (statusPriority[right.status] ?? 9);
      }
      if (sortMode === 'queue') {
        return Number(right.queue_count || 0) - Number(left.queue_count || 0);
      }
      const leftTime = left.created_at ? new Date(left.created_at).getTime() : 0;
      const rightTime = right.created_at ? new Date(right.created_at).getTime() : 0;
      return leftTime - rightTime || left.id - right.id;
    });
  }, [deviceTypeFilter, devices, searchText, sortMode, statusFilter]);

  const selectedConfig = selectedDevice
    ? statusConfig[selectedDevice.status] || statusConfig.offline
    : statusConfig.offline;
  const selectedIsConfocal = isConfocalDevice(selectedDevice);
  const selectedOlympus = selectedDevice?.metrics?.olympus || {};
  const selectedProgress = selectedDevice?.task_progress == null ? null : Number(selectedDevice.task_progress);
  const selectedTaskCompleted = selectedDevice?.status === 'idle' && selectedProgress === 100;
  const currentResultReady = Boolean(
    selectedDevice
    && selectedDevice.status !== 'offline'
    && selectedDevice.client_base_url
    && selectedProgress === 100
  );
  const activeQueueEntry = getActiveQueueEntry(queue);
  const selectedUsesConfocalQuota = selectedDevice?.metrics?.device_type === 'laser_confocal';
  const queueQuota = selectedUsesConfocalQuota ? 2 : 3;
  const myQueueEntries = queue.filter(record => (
    record.created_by_id && record.created_by_id === queueUserIdRef.current
  ));
  const myNextQueueEntry = myQueueEntries[0] || null;

  const renderRecentResults = () => {
    if (resultsLoading) return <Skeleton active paragraph={{ rows: 4 }} />;
    if (resultsAvailability === 'offline') {
      return <Alert type="warning" showIcon message="设备已离线" description="恢复连接后才能读取设备上的最近结果。" />;
    }
    if (resultsAvailability === 'unconfigured') {
      return <Alert type="info" showIcon message="尚未配置结果服务" description="请先在设备管理中配置客户端结果服务地址。" />;
    }
    if (resultsError) {
      return (
        <Alert
          type="error"
          showIcon
          message="最近结果加载失败"
          description={resultsError}
          action={<Button size="small" onClick={() => fetchRecentResults(selectedDevice)}>重试</Button>}
        />
      );
    }
    if (!recentResults.length) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="设备暂无历史结果" />;
    }
    return (
      <List
        className="monitor-result-list"
        dataSource={recentResults}
        renderItem={item => (
          <List.Item>
            <div className="monitor-result-item">
              <div className="monitor-result-item__main">
                <strong>{item.task_name || item.folder || '未命名结果'}</strong>
                <span>{item.updated_at ? formatDateTime(item.updated_at) : item.folder || '-'}</span>
              </div>
              <Space size={4} wrap>
                {!selectedIsConfocal ? (
                  <Button
                    size="small"
                    icon={<FileTextOutlined />}
                    disabled={!item.folder}
                    onClick={() => setTableModal({ open: true, folder: item.folder })}
                  >
                    表格
                  </Button>
                ) : null}
                <Button
                  size="small"
                  icon={<FileImageOutlined />}
                  disabled={!item.folder}
                  onClick={() => setImagesModal({ open: true, folder: item.folder })}
                >
                  图片
                </Button>
                {selectedIsConfocal ? (
                  <Button
                    type="link"
                    size="small"
                    icon={<FolderOpenOutlined />}
                    disabled={!item.folder}
                    onClick={() => openCleanupModal(item.folder)}
                  >
                    整理
                  </Button>
                ) : null}
              </Space>
            </div>
          </List.Item>
        )}
      />
    );
  };

  const renderQueueActivity = () => {
    if (queueLoading && !queueLogs.length) return <Skeleton active paragraph={{ rows: 3 }} />;
    if (!queueLogs.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今日暂无排队动态" />;
    return (
      <div className="monitor-activity-grid">
        {queueLogs.map(log => {
          const logDisplay = getQueueLogDisplay(log);
          const isMine = log.changed_by_id && log.changed_by_id === queueUserIdRef.current;
          return (
            <div className="monitor-activity-item" key={log.id}>
              <span className="monitor-activity-item__dot" style={{ background: logDisplay.color || '#98a2b3' }} />
              <div className="monitor-activity-item__copy">
                <strong style={logDisplay.color ? { color: logDisplay.color } : undefined} title={logDisplay.text}>
                  {logDisplay.text}
                </strong>
                <div className="monitor-activity-item__meta">
                  <time title={formatDateTime(log.change_time)}>{formatTime(log.change_time)}</time>
                  <Tooltip title={log.changed_by_id ? `浏览器标识：${log.changed_by_id}` : undefined}>
                    <span className="monitor-activity-item__actor">
                      {log.changed_by || '系统'}
                      {isMine ? <em>本人</em> : null}
                    </span>
                  </Tooltip>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <DndProvider backend={HTML5Backend}>
      {modalContextHolder}
      <div className="analytics-page monitor-page">
        <Card className="analytics-filter-card monitor-filter-card">
          <div className="monitor-filter-bar">
            <Input
              allowClear
              prefix={<SearchOutlined />}
              value={searchText}
              placeholder="搜索设备名称、编号、型号或位置"
              onChange={event => setSearchText(event.target.value)}
            />
            <Select
              value={statusFilter}
              onChange={setStatusFilter}
              options={[
                { value: 'all', label: '全部状态' },
                { value: 'busy', label: '检测中' },
                { value: 'idle', label: '空闲' },
                { value: 'attention', label: '需要关注' },
                { value: 'offline', label: '离线' },
              ]}
            />
            <Select
              value={deviceTypeFilter}
              onChange={setDeviceTypeFilter}
              options={[
                { value: 'all', label: '全部类型' },
                { value: 'standard', label: '普通设备' },
                { value: 'confocal', label: '激光共聚焦' },
              ]}
            />
            <Select
              value={sortMode}
              onChange={setSortMode}
              options={[
                { value: 'default', label: '按设备顺序' },
                { value: 'attention', label: '需关注优先' },
                { value: 'queue', label: '排队人数优先' },
              ]}
            />
            <div className="monitor-filter-bar__meta">
              <span className="monitor-filter-bar__count">
                显示 <strong>{filteredDevices.length}</strong> / {devices.length} 台
              </span>
              <span className="monitor-filter-bar__sync">
                {lastUpdatedAt ? '同步于 ' + formatTime(lastUpdatedAt) : '等待同步'}
              </span>
              <Button
                type="text"
                size="small"
                aria-label="刷新设备状态"
                icon={<ReloadOutlined />}
                loading={devicesLoading || queueLoading}
                onClick={handleRefreshAll}
              />
            </div>
          </div>
        </Card>

        {devicesError ? (
          <Alert
            type="error"
            showIcon
            message="设备状态加载失败"
            description={devicesError}
            action={<Button size="small" onClick={fetchDevices}>重试</Button>}
          />
        ) : null}

        {devicesLoading && !devices.length ? (
          <div className="monitor-device-grid">
            {[1, 2, 3, 4].map(item => (
              <Card key={item} className="monitor-device-card-skeleton">
                <Skeleton active paragraph={{ rows: 4 }} />
              </Card>
            ))}
          </div>
        ) : filteredDevices.length ? (
          <div className="monitor-device-grid">
            {filteredDevices.map(device => (
              <DeviceOverviewCard
                key={device.id}
                device={device}
                selected={device.id === selectedDeviceId}
                onSelect={handleSelectDevice}
                onQuickQueue={handleQuickQueue}
              />
            ))}
          </div>
        ) : (
          <Card className="analytics-panel">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={devices.length ? '没有符合筛选条件的设备' : '尚未登记设备'}
            >
              {devices.length ? (
                <Button onClick={() => {
                  setSearchText('');
                  setStatusFilter('all');
                  setDeviceTypeFilter('all');
                }}>
                  清除筛选
                </Button>
              ) : null}
            </Empty>
          </Card>
        )}

        {selectedDevice ? (
          <Card className="analytics-panel monitor-workbench">
            <div className="monitor-workbench-summary">
              <div className="monitor-workbench-summary__identity">
                <span>当前操作设备</span>
                <div className="monitor-workbench-summary__identity-title">
                  <Typography.Title level={4}>{selectedDevice.name}</Typography.Title>
                  <Badge
                    status={selectedConfig.color}
                    text={selectedDevice.status === 'offline' ? '离线' : selectedConfig.text}
                  />
                </div>
                <Space size={6} wrap>
                  <Tag>{selectedDevice.device_code || '无设备编号'}</Tag>
                  <Tag color={selectedIsConfocal ? 'purple' : 'blue'}>
                    {selectedIsConfocal ? '激光共聚焦' : '普通设备'}
                  </Tag>
                </Space>
              </div>

              <div className="monitor-workbench-summary__metrics">
                <div className="monitor-workbench-progress">
                  <span>{selectedDevice.task_name || (selectedTaskCompleted ? '最近任务' : '任务进度')}</span>
                  <strong>{Number.isFinite(selectedProgress) ? Math.max(0, Math.min(100, selectedProgress)) + '%' : '-'}</strong>
                  <Progress
                    percent={Number.isFinite(selectedProgress) ? Math.max(0, Math.min(100, selectedProgress)) : 0}
                    status={selectedDevice.status === 'error' ? 'exception' : selectedProgress === 100 ? 'success' : 'active'}
                    showInfo={false}
                    aria-label={`${selectedDevice.name} 当前任务进度 ${Number.isFinite(selectedProgress) ? Math.max(0, Math.min(100, selectedProgress)) : 0}%`}
                  />
                </div>
                <div className="monitor-workbench-queue-count">
                  <span>当前排队</span>
                  <strong>{queueLoading ? '…' : queue.length}</strong>
                  <small>人</small>
                </div>
              </div>

              <div className="monitor-workbench-actions">
                <Select
                  className="monitor-workbench-switcher"
                  value={selectedDevice.id}
                  onChange={handleSelectDevice}
                  optionFilterProp="label"
                  showSearch
                  options={devices.map(device => ({
                    value: device.id,
                    label: `${device.name} · ${(statusConfig[device.status] || statusConfig.offline).text}`,
                  }))}
                />
                <div className="monitor-completion-notify-control">
                  <Dropdown
                    trigger={['click']}
                    placement="bottomRight"
                    menu={{
                      items: completionNotifyMenuItems,
                      selectable: true,
                      selectedKeys: [notifyMode],
                      onClick: handleNotifyModeSelect,
                    }}
                  >
                    <Button
                      className={`monitor-completion-notify monitor-completion-notify--${notifyMode}`}
                      type="text"
                      size="small"
                      icon={notifyMode === 'always' ? <ClockCircleFilled /> : <ClockCircleOutlined />}
                      title={`完成提醒：当前${notifyModeLabel}，点击设置`}
                      aria-label={`完成提醒：当前${notifyModeLabel}，点击选择提醒模式`}
                      aria-pressed={notifyMode !== 'off'}
                    />
                  </Dropdown>
                </div>
              </div>
            </div>

            <QueueTimeoutNotice
              device={selectedDevice}
              queueCount={queueLoading ? Number(selectedDevice.queue_count || 0) : queue.length}
              extending={extendingDeviceId === selectedDevice.id}
              onExtend={() => handleExtendTimeout(selectedDevice.id)}
            />

            {myNextQueueEntry ? (
              <div className="monitor-my-queue-state">
                <CheckCircleOutlined />
                <span>
                  {myNextQueueEntry.position === 1
                    ? '现在已轮到您，请开始使用设备'
                    : `您已在队列中，当前排第 ${getQueuePositionDisplay(myNextQueueEntry.position)} 位`}
                </span>
                {myQueueEntries.length > 1 ? <Tag color="blue">共 {myQueueEntries.length} 份</Tag> : null}
              </div>
            ) : null}

            <div className="monitor-workspace-grid">
              <div
                ref={queueFormAnchorRef}
                className={`monitor-queue-anchor${queueFocusPulse ? ' monitor-queue-anchor--focused' : ''}`}
              >
                <Card
                  className="monitor-workspace-card monitor-queue-card"
                  title={(
                    <div className="analytics-panel__title">
                      <span>排队管理</span>
                      <span className="analytics-panel__hint">
                        {activeQueueEntry
                          ? isUnclaimedPlaceholder(activeQueueEntry)
                            ? '当前使用人待认领'
                            : `当前使用：${activeQueueEntry.inspector_name}`
                          : '暂无正在使用人员'}
                      </span>
                    </div>
                  )}
                  extra={<span className="monitor-card-count">共 {queue.length} 人</span>}
                >
                  <Form
                    form={form}
                    layout="vertical"
                    className="monitor-queue-form"
                    onFinish={handleJoinQueue}
                    onValuesChange={(_, values) => setInspectorName(values.inspector_name || '')}
                  >
                    <Form.Item
                      name="inspector_name"
                      label="检验员"
                      rules={[{ required: true, message: '请输入检验员姓名' }]}
                    >
                      <Input placeholder="输入姓名" maxLength={50} />
                    </Form.Item>
                    <Form.Item
                      name="copies"
                      label="排队份数"
                      initialValue={1}
                      rules={[{ type: 'number', min: 1, max: queueQuota, message: `同类型设备合计最多可排 ${queueQuota} 份` }]}
                    >
                      <InputNumber min={1} max={queueQuota} />
                    </Form.Item>
                    <Form.Item label=" ">
                      <Button
                        type="primary"
                        htmlType="submit"
                        icon={<PlusOutlined />}
                        loading={queueSubmitting}
                      >
                        加入排队
                      </Button>
                    </Form.Item>
                  </Form>
                  <div className="monitor-queue-form-meta">
                    <div className="monitor-queue-form__hint">
                      {selectedDevice.status === 'offline' ? '设备当前离线，仍可提前排队 · ' : ''}
                      {selectedUsesConfocalQuota
                        ? '同一浏览器在全部共聚焦设备合计最多排 2 份'
                        : '同一浏览器在全部普通设备合计最多排 3 份'}
                      {' · '}加入后轮到您会自动提醒
                    </div>
                  </div>

                  {queueError ? (
                    <Alert
                      type="error"
                      showIcon
                      message="排队信息加载失败"
                      description={queueError}
                      action={<Button size="small" onClick={() => fetchQueue(selectedDevice.id)}>重试</Button>}
                    />
                  ) : null}

                  <DragTable
                    className="monitor-queue-table"
                    dataSource={queue}
                    columns={queueColumns}
                    rowKey="id"
                    loading={queueLoading}
                    onDropConfirm={handleDropConfirm}
                    scroll={{ x: 620 }}
                    locale={{
                      emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前无人排队" />,
                    }}
                  />
                </Card>
              </div>

              <Card
                className="monitor-workspace-card monitor-results-card"
                title={(
                  <div className="analytics-panel__title">
                    <span>结果查看</span>
                    <span className="analytics-panel__hint">当前结果与最近 5 次记录</span>
                  </div>
                )}
              >
                <div className="monitor-results-panel">
                  <div className="monitor-current-result">
                    <div>
                      <strong>当前任务结果</strong>
                      <span>
                        {selectedProgress === 100
                          ? currentResultReady
                            ? '检测已完成，可查看本次结果'
                            : '检测已完成，但结果服务暂不可用'
                          : '检测完成至 100% 后开放'}
                      </span>
                    </div>
                    <Space wrap>
                      {!selectedIsConfocal ? (
                        <Button
                          type="primary"
                          icon={<FileTextOutlined />}
                          disabled={!currentResultReady}
                          onClick={() => setTableModal({ open: true, folder: null })}
                        >
                          查看表格
                        </Button>
                      ) : null}
                      <Button
                        icon={<FileImageOutlined />}
                        disabled={!currentResultReady}
                        onClick={() => setImagesModal({ open: true, folder: null })}
                      >
                        查看图片
                      </Button>
                      {selectedIsConfocal ? (
                        <Button
                          type="link"
                          icon={<FolderOpenOutlined />}
                          disabled={!currentResultReady}
                          onClick={() => openCleanupModal(null)}
                        >
                          整理结果文件…
                        </Button>
                      ) : null}
                    </Space>
                  </div>
                  <div className="monitor-section-label">最近 5 次结果</div>
                  {renderRecentResults()}
                </div>
              </Card>
            </div>

            <Collapse
              className="monitor-workbench-activity"
              items={[{
                key: 'activity',
                label: `今日动态 (${queueLogs.length})`,
                children: renderQueueActivity(),
              }]}
            />

            <Collapse
              className="monitor-device-detail-collapse"
              items={[{
                key: 'details',
                label: selectedIsConfocal ? '共聚焦采集详情与设备信息' : '任务与设备详情',
                children: (
                  <div className="monitor-device-details">
                    <div className="monitor-workbench-context__facts">
                      <span><small>位置</small>{selectedDevice.location || '-'}</span>
                      <span><small>型号</small>{selectedDevice.model || '-'}</span>
                      <span><small>最近心跳</small>{selectedDevice.last_heartbeat ? formatRelativeTime(selectedDevice.last_heartbeat) : '-'}</span>
                      <span><small>设备温度</small>{formatTemperature(selectedDevice.metrics?.temperature)}</span>
                    </div>

                    {selectedDevice.task_name && selectedProgress != null && selectedDevice.status !== 'offline' ? (
                      <div className="monitor-current-task">
                        <div>
                          <span>{selectedTaskCompleted ? '最近完成' : '当前任务'}</span>
                          <strong>{selectedDevice.task_name}</strong>
                          {selectedTaskCompleted ? (
                            <small>任务已完成，可在“结果查看”中打开</small>
                          ) : selectedDevice.task_elapsed_seconds != null ? (
                            <small>已运行 {Math.max(0, Math.floor(selectedDevice.task_elapsed_seconds / 60))} 分钟</small>
                          ) : null}
                        </div>
                        <Progress
                          percent={Math.max(0, Math.min(100, selectedProgress))}
                          status={selectedDevice.status === 'error' ? 'exception' : selectedProgress === 100 ? 'success' : 'active'}
                        />
                      </div>
                    ) : null}

                    {selectedIsConfocal ? (
                      <div className="monitor-confocal-panel">
                        <div className="monitor-confocal-panel__heading">
                          <strong>共聚焦采集详情</strong>
                          <span>{getOlympusDisplayState(selectedOlympus, selectedDevice.status)}</span>
                        </div>
                        <div className="monitor-confocal-grid">
                          <span><small>组进度</small>{selectedOlympus.group_completed || 0} / {selectedOlympus.group_total || '-'}</span>
                          <span><small>图像进度</small>{selectedOlympus.image_progress != null ? selectedOlympus.image_progress + '%' : '-'}</span>
                          <span><small>当前帧</small>{selectedOlympus.frame_current || '-'} / {selectedOlympus.frame_total || '-'}</span>
                          <span><small>XY 位置</small>{selectedOlympus.xy_position ? selectedOlympus.xy_position.x + ', ' + selectedOlympus.xy_position.y : '-'}</span>
                          <span><small>Z 位置</small>{selectedOlympus.z_position ?? '-'}</span>
                          <span><small>Z 范围</small>{selectedOlympus.z_range ? selectedOlympus.z_range.start + ' – ' + selectedOlympus.z_range.end : '-'}</span>
                        </div>
                        {selectedOlympus.current_file ? (
                          <div className="monitor-confocal-path">
                            <small>当前文件</small><Typography.Text copyable>{selectedOlympus.current_file}</Typography.Text>
                          </div>
                        ) : null}
                        {getValidOutputPath(selectedOlympus.output_path) ? (
                          <div className="monitor-confocal-path">
                            <small>输出目录</small>
                            <Typography.Text copyable={{ text: getValidOutputPath(selectedOlympus.output_path) }} ellipsis>
                              {getValidOutputPath(selectedOlympus.output_path)}
                            </Typography.Text>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                ),
              }]}
            />
          </Card>
        ) : null}

        {!selectedIsConfocal ? (
          <ResultsModal
            open={tableModal.open}
            title={(selectedDevice?.name || '设备') + ' · 结果表格'}
            url={buildResultsUrl('table', tableModal.folder)}
            onClose={() => setTableModal({ open: false, folder: null })}
          />
        ) : null}

        <Modal
          title={(selectedDevice?.name || '设备') + ' · 结果图片'}
          open={imagesModal.open}
          onCancel={() => setImagesModal({ open: false, folder: null })}
          afterOpenChange={open => {
            if (open) setImagesLayoutVersion(version => version + 1);
          }}
          footer={null}
          width="90vw"
          style={{ top: 20 }}
          styles={{ body: { height: '80vh', padding: 0, width: '100%', overflow: 'hidden' } }}
          destroyOnClose
        >
          {imagesModal.open && selectedDeviceId ? (
            <ResultsImages
              deviceId={selectedDeviceId}
              folder={imagesModal.folder}
              embedded
              layoutVersion={imagesLayoutVersion}
            />
          ) : null}
        </Modal>

        <Modal
          title="整理共聚焦结果文件"
          open={cleanupModal.open}
          onCancel={closeCleanupModal}
          onOk={handleCleanupSubmit}
          okText="确认整理"
          cancelText="取消"
          confirmLoading={cleanupModal.submitting}
          okButtonProps={{ danger: true }}
          maskClosable={!cleanupModal.submitting}
          keyboard={!cleanupModal.submitting}
          destroyOnClose={false}
        >
          <div className="monitor-cleanup-dialog">
            <Alert
              type="warning"
              showIcon
              message="文件整理规则"
              description="仅保留以 _I.jpg 结尾的图片，其余图片会移动到输出目录的 .recycle 文件夹。"
            />
            <div>
              <small>设备</small>
              <strong>{selectedDevice?.name || '-'}</strong>
            </div>
            <div>
              <small>原文件夹名称（可复制）</small>
              <Typography.Text copyable={{ text: cleanupModal.sourceFolderName || '-' }}>
                {cleanupModal.sourceFolderName || '-'}
              </Typography.Text>
            </div>
            <Checkbox
              checked={cleanupModal.renameEnabled}
              onChange={event => {
                const checked = event.target.checked;
                setCleanupModal(prev => ({
                  ...prev,
                  renameEnabled: checked,
                  newFolderName: checked && !prev.newFolderName
                    ? getDefaultRenameName(prev.sourceFolderName)
                    : prev.newFolderName,
                }));
              }}
            >
              同时重命名文件夹
            </Checkbox>
            <Input
              placeholder="请输入新文件夹名称"
              disabled={!cleanupModal.renameEnabled}
              value={cleanupModal.newFolderName}
              maxLength={128}
              onChange={event => setCleanupModal(prev => ({ ...prev, newFolderName: event.target.value }))}
            />
          </div>
        </Modal>

        <Modal
          title="认领占位人员"
          open={claimModal.open}
          onCancel={closeClaimModal}
          maskClosable={!claimModal.submittingAction}
          keyboard={!claimModal.submittingAction}
          footer={[
            <Button
              key="delete"
              danger
              onClick={handleDeletePlaceholder}
              loading={claimModal.submittingAction === 'delete'}
              disabled={Boolean(claimModal.submittingAction && claimModal.submittingAction !== 'delete')}
            >
              删除占位
            </Button>,
            <Button key="cancel" onClick={closeClaimModal} disabled={Boolean(claimModal.submittingAction)}>
              取消
            </Button>,
            <Button
              key="claim"
              type="primary"
              onClick={handleClaimPlaceholder}
              loading={claimModal.submittingAction === 'claim'}
              disabled={Boolean(claimModal.submittingAction && claimModal.submittingAction !== 'claim')}
            >
              确认认领
            </Button>,
          ]}
        >
          <Form form={claimForm} layout="vertical">
            <Form.Item
              label="检验员姓名"
              name="inspector_name"
              rules={[{ required: true, message: '请输入检验员姓名' }]}
            >
              <Input placeholder="检验员姓名" maxLength={50} />
            </Form.Item>
          </Form>
        </Modal>
      </div>
    </DndProvider>
  );
}

export default DeviceMonitor;
