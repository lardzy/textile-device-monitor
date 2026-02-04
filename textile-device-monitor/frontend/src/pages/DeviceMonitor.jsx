import { useState, useEffect, useMemo, useRef } from 'react';
import { DndProvider, useDrag, useDrop } from 'react-dnd';
import { HTML5Backend } from 'react-dnd-html5-backend';
import { Card, Row, Col, Badge, Tag, Progress, Form, Input, InputNumber, Button, Table, List, Select, message, Modal, Tooltip } from 'antd';
import { CheckCircleOutlined, ClockCircleOutlined, ClockCircleFilled, ExclamationCircleOutlined, LoadingOutlined, StopOutlined, PlusOutlined, DeleteOutlined, HolderOutlined } from '@ant-design/icons';
import { deviceApi } from '../api/devices';
import { queueApi } from '../api/queue';
import { resultsApi } from '../api/results';
import ResultsModal from '../components/ResultsModal';
import ResultsImages from './ResultsImages';
import wsClient from '../websocket/client';
import { formatRelativeTime, formatDateTime, formatTime } from '../utils/dateHelper';
import { addQueueNoticeEntry, getInspectorName, getOrCreateQueueUserId, getQueueNoticeModes, removeQueueNoticeEntry, saveInspectorName, saveQueueNoticeModes } from '../utils/localStorage';


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

const formatUserIdShort = (value) => {
  if (!value) return '-';
  const text = String(value);
  if (text.length <= 10) return text;
  return text.slice(0, 8);
};

const renderUserId = (value) => {
  if (!value) return '-';
  const shortId = formatUserIdShort(value);
  return (
    <Tooltip title={String(value)}>
      <span>{shortId}</span>
    </Tooltip>
  );
};

const renderUserLabel = (name, userId) => {
  const label = name || '-';
  if (!userId) return label;
  const shortId = formatUserIdShort(userId);
  return (
    <span>
      {label} (
      <Tooltip title={String(userId)}>
        <span>{shortId}</span>
      </Tooltip>
      )
    </span>
  );
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

const type = 'queue-row';

const isConfocalDevice = (device) => {
  if (!device) return false;
  return device.metrics?.device_type === 'laser_confocal' || Boolean(device.metrics?.olympus);
};

const DraggableRow = ({ index, moveRow, onDropConfirm, isActive, children, ...restProps }) => {
  const ref = useRef(null);
  const [{ isOver, dropClassName }, drop] = useDrop({
    accept: type,
    collect: (monitor) => {
      const { index: dragIndex } = monitor.getItem() || {};
      if (dragIndex === index) {
        return {};
      }
      return {
        isOver: monitor.isOver(),
        dropClassName: 'drop-over-downward',
      };
    },
    drop: (item) => {
      const dragIndex = item.index;
      if (dragIndex === index) {
        return;
      }
      onDropConfirm(dragIndex, index);
    },
  });
  const [, drag] = useDrag({
    type,
    item: { index },
    collect: (monitor) => ({
      isDragging: monitor.isDragging(),
    }),
  });
  drop(drag(ref));
  const rowStyle = {
    ...restProps.style,
    cursor: 'move',
    ...(isActive ? { background: '#f6ffed' } : null),
  };
  return (
    <tr
      ref={ref}
      className={`${isOver ? dropClassName : ''}`}
      style={rowStyle}
      {...restProps}
    >
      {children}
    </tr>
  );
};

const DragTable = ({ columns, dataSource, onDropConfirm, ...props }) => {
  const moveRow = (dragIndex, hoverIndex) => {
    const dragRow = dataSource[dragIndex];
    const newData = [...dataSource];
    newData.splice(dragIndex, 1);
    newData.splice(hoverIndex, 0, dragRow);
    return newData;
  };

  const components = {
    body: {
      row: (props) => {
        const index = dataSource.findIndex((x) => x.id === props['data-row-key']);
        const record = index >= 0 ? dataSource[index] : null;
        return (
          <DraggableRow
            index={index}
            moveRow={moveRow}
            onDropConfirm={onDropConfirm}
            isActive={record?.position === 1}
            {...props}
          />
        );
      },
    },
  };

  return <Table columns={columns} dataSource={dataSource} components={components} pagination={false} size="small" {...props} />;
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

function DeviceMonitor() {
  const [devices, setDevices] = useState([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState(null);
  const [queue, setQueue] = useState([]);
  const [queueLogs, setQueueLogs] = useState([]);
  const [inspectorName, setInspectorName] = useState(getInspectorName());
  const [tableModal, setTableModal] = useState({ open: false, folder: null });
  const [imagesModal, setImagesModal] = useState({ open: false, folder: null });
  const [recentResults, setRecentResults] = useState([]);
  const [notifyModes, setNotifyModes] = useState(() => getQueueNoticeModes());
  const [nowTime, setNowTime] = useState(Date.now());
  const [form] = Form.useForm();
  const devicesRef = useRef([]);
  const notifyModesRef = useRef(notifyModes);
  const lastProgressRef = useRef(new Map());
  const lastQueueCompletionRef = useRef(new Map());
  const activeQueueRef = useRef(new Map());
  const lastDeviceNotificationRef = useRef(new Map());
  const deviceNotifyTimersRef = useRef(new Map());
  const queueUserIdRef = useRef(getOrCreateQueueUserId());


  const selectedDevice = useMemo(() => {
    return devices.find(device => device.id === selectedDeviceId) || null;
  }, [devices, selectedDeviceId]);

  useEffect(() => {
    devicesRef.current = devices;
  }, [devices]);

  useEffect(() => {
    notifyModesRef.current = notifyModes;
    saveQueueNoticeModes(notifyModes);
  }, [notifyModes]);

  useEffect(() => {
    const timer = setInterval(() => {
      setNowTime(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const fetchDevices = async () => {
    try {
      const data = await deviceApi.getAll();
      const sorted = data
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
          return {
            ...existing,
            ...item,
            queue_count: item.queue_count ?? existing.queue_count,
          };
        });
      });
      if (!selectedDeviceId && sorted.length > 0) {
        setSelectedDeviceId(sorted[0].id);
      }
    } catch (error) {
      console.error('Failed to fetch devices:', error);
    }
  };

  const fetchQueue = async (deviceId, options = {}) => {
    const { notify = false, reason, updateState = true } = options;
    if (!deviceId) return;
    try {
      const data = await queueApi.getByDevice(deviceId);
      const sortedQueue = (data.queue || [])
        .slice()
        .sort((a, b) => a.position - b.position);
      const sortedLogs = (data.logs || []).slice().sort((a, b) => new Date(b.change_time) - new Date(a.change_time));
      if (updateState) {
        setQueue(sortedQueue);
        setQueueLogs(sortedLogs);
      }
      syncQueueNoticeEntries(sortedQueue, deviceId);
      if (notify) {
        await notifyActiveQueueEntry(sortedQueue, deviceId, reason);
      } else {
        syncActiveQueueEntry(deviceId, sortedQueue);
      }
    } catch (error) {
      message.error('获取排队列表失败');
    }
  };

  const fetchRecentResults = async (device) => {
    if (!device?.id) {
      setRecentResults([]);
      return;
    }
    if (!device.client_base_url || device.status === 'offline') {
      setRecentResults([]);
      return;
    }
    try {
      const data = await resultsApi.getRecent(device.id, 5);
      setRecentResults(data.items || []);
    } catch (error) {
      setRecentResults([]);
    }
  };

  const requestNotificationPermission = async () => {
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
  };

  const showPersistentNotice = (title, content) => {
    Modal.confirm({
      title,
      content,
      okText: '我知道了',
      cancelButtonProps: { style: { display: 'none' } },
      maskClosable: false,
      closable: false,
      keyboard: false,
      centered: true,
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
      if (record?.created_by_id && record.created_by_id === userId) {
        addQueueNoticeEntry({
          id: record.id,
          device_id: deviceId,
          inspector_name: record.inspector_name,
          created_by_id: userId,
        });
      }
    });
  };

  const notifyActiveQueueEntry = async (queueList, deviceId, reason) => {
    if (deviceId == null) return;
    const activeEntry = getActiveQueueEntry(queueList);
    const activeId = activeEntry ? activeEntry.id : null;
    const previousId = activeQueueRef.current.get(deviceId);
    activeQueueRef.current.set(deviceId, activeId);

    if (!activeEntry || activeId == null || activeId === previousId) {
      return;
    }

    const userId = queueUserIdRef.current;
    if (!userId || activeEntry.created_by_id !== userId) {
      return;
    }

    removeQueueNoticeEntry(activeId);

    const deviceName = devicesRef.current.find(device => device.id === deviceId)?.name || '';
    const inspectorName = activeEntry.inspector_name || '检验员';
    showPersistentNotice(
      '排队提醒',
      `${deviceName || '设备'} - ${inspectorName} 已轮到`
    );

    const permitted = await requestNotificationPermission();
    if (!permitted) {
      return;
    }
    sendQueueNotification(activeEntry, deviceName);

    if (reason === 'complete') {
      lastQueueCompletionRef.current.set(deviceId, Date.now());
      const timer = deviceNotifyTimersRef.current.get(deviceId);
      if (timer) {
        clearTimeout(timer);
        deviceNotifyTimersRef.current.delete(deviceId);
      }
    }
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
      const lastQueueTime = lastQueueCompletionRef.current.get(device.id);
      if (lastQueueTime && Date.now() - lastQueueTime < 2000) {
        return;
      }
      showPersistentNotice(
        '检测完成提醒',
        `${device.name || '设备'} 检测完成`
      );
      const permitted = await requestNotificationPermission();
      if (!permitted) {
        return;
      }
      sendDeviceNotification(device);
      lastDeviceNotificationRef.current.set(device.id, Date.now());
      if (mode === 'once') {
        setNotifyModes(prev => ({
          ...prev,
          [String(device.id)]: 'off'
        }));
      }
    }, 600);
    timers.set(device.id, timerId);
  };

  const handleExtendTimeout = async (deviceId) => {
    if (!deviceId) return;
    const changedBy = inspectorName?.trim() || '系统';
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
      if (selectedDeviceId === deviceId) {
        fetchQueue(deviceId);
      }
    } catch (error) {
      message.error(error.message || '延长超时失败');
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
      if (permitted) {
        sendCustomNotification('排队提醒', content);
      }
    }

    if (payload.next_created_by_id && payload.next_created_by_id === userId) {
      const content = `${deviceName} 当前使用人未开始，请注意顺位变化`;
      showPersistentNotice('排队提醒', content);
      const permitted = await requestNotificationPermission();
      if (permitted) {
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
      if (permitted) {
        sendCustomNotification('排队提醒', content);
      }
    }
  };

  const notifyQueueCompletion = async (payload) => {
    if (!payload) return;
    const userId = queueUserIdRef.current;
    if (!userId) return;
    if (payload.completed_by_id && payload.completed_by_id === userId) {
      const deviceName = payload.device_name || '设备';
      const inspectorName = payload.completed_by || '检验员';
      const content = `${deviceName} 检测完成，${inspectorName} 已从排队移除`;
      showPersistentNotice('检测完成提醒', content);
      const permitted = await requestNotificationPermission();
      if (permitted) {
        sendCustomNotification('检测完成提醒', content);
      }
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

    wsClient.on('device_status_update', (data) => {
      if (!data || data.device_id == null) return;
      setDevices(prev => prev.map(device => 
        device.id === data.device_id 
          ? { ...device, ...data }
          : device
      ));
    });

    wsClient.on('queue_timeout_update', (data) => {
      if (!data || data.device_id == null) return;
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
      if (data.action === 'delete') {
        setDevices(prev => prev.filter(device => device.id !== data.device_id));
        if (selectedDeviceId === data.device_id) {
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
      setDevices(prev => prev.map(device => 
        device.id === data.device_id 
          ? { ...device, status: 'offline' }
          : device
      ));
    });

    wsClient.on('queue_update', (data) => {
      if (!data || data.device_id == null) return;
      if (data.action === 'complete') {
        notifyQueueCompletion(data);
      }
      if (data.action === 'leave' && data.queue_id) {
        removeQueueNoticeEntry(data.queue_id);
      }
      if (data.device_id === selectedDeviceId) {
        fetchQueue(selectedDeviceId, { notify: true, reason: data.action });
      } else {
        fetchQueue(data.device_id, { notify: true, reason: data.action, updateState: false });
      }
      setDevices(prev => prev.map(device =>
        device.id === data.device_id
          ? { ...device, queue_count: data.queue_count ?? device.queue_count }
          : device
      ));
    });

    const pollId = setInterval(() => {
      fetchDevices();
      if (selectedDeviceId) {
        fetchQueue(selectedDeviceId);
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
  }, [selectedDeviceId]);

  useEffect(() => {
    setRecentResults([]);
    if (selectedDeviceId) {
      fetchQueue(selectedDeviceId);
      fetchRecentResults(selectedDevice);
    }
    setTableModal({ open: false, folder: null });
    setImagesModal({ open: false, folder: null });
  }, [selectedDeviceId]);

  useEffect(() => {
    if (!selectedDeviceId) return;
    const pollId = setInterval(() => {
      const device = devicesRef.current.find(item => item.id === selectedDeviceId);
      fetchRecentResults(device);
    }, 60000);
    return () => clearInterval(pollId);
  }, [selectedDeviceId]);

  const buildResultsUrl = (type, folder) => {
    if (!selectedDevice?.id) return '';
    const basePath = type === 'table' ? '/results/table' : '/results/images';
    const folderParam = folder ? `&folder=${encodeURIComponent(folder)}` : '';
    return `${basePath}?device_id=${selectedDevice.id}${folderParam}`;
  };

  const handleCleanupImages = (folder) => {
    if (!selectedDevice?.id) return;
    Modal.confirm({
      title: '删除多余图片',
      content: '仅保留以“_I.jpg”结尾的图片，其余图片会移动到输出目录的 .recycle 文件夹。',
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const data = await resultsApi.cleanupImages(selectedDevice.id, folder || undefined);
          const moved = data?.moved ?? 0;
          message.success(`已移动 ${moved} 张图片`);
        } catch (error) {
          const msg = error?.message || '';
          if (msg.includes('folder_not_found')) {
            message.error('输出路径不存在，无法清理');
          } else if (msg.includes('output_parent_missing')) {
            message.error('输出路径缺少父级目录，无法清理');
          } else if (msg.includes('cleanup_not_supported')) {
            message.error('当前设备不支持清理');
          } else {
            message.error('删除多余图片失败');
          }
        }
      }
    });
  };


  const handleJoinQueue = async (values) => {
    try {
      const records = await queueApi.join({
        inspector_name: values.inspector_name,
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
            inspector_name: values.inspector_name,
            created_by_id: queueUserIdRef.current,
          });
        }
      }

      if (actualCopies > 0) {
        message.success(`加入排队成功 (${actualCopies}份)`);
      }
      if (actualCopies > 0 && actualCopies < requestedCopies) {
        message.info(`已按配额加入 ${actualCopies} 份（超出部分忽略）`);
      }
      saveInspectorName(values.inspector_name);
      form.resetFields();
      setInspectorName('');
      fetchQueue(selectedDeviceId, { notify: true, reason: 'join' });
    } catch (error) {
      message.error(error?.message || '加入排队失败');
    }
  };

  const handleChangePosition = async (record, newPosition) => {
    const fromLabel = getQueuePositionLabel(record.position);
    const toLabel = getQueuePositionLabel(newPosition);
    Modal.confirm({
      title: '确认移动位置',
      content: `确定要将 ${record.inspector_name} 从 ${fromLabel} 移动到 ${toLabel} 吗？`,
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        try {
          const changedBy = inspectorName?.trim() || '系统';
          await queueApi.updatePosition(record.id, {
            new_position: newPosition,
            changed_by: changedBy,
            version: record.version,
            changed_by_id: queueUserIdRef.current,
          });
          message.success('修改位置成功');
          fetchQueue(selectedDeviceId, { notify: true, reason: 'position_change' });
        } catch (error) {
          if (error.response?.status === 409) {
            message.error('该记录已被其他用户修改，请刷新后重试');
            fetchQueue(selectedDeviceId);
          } else {
            message.error(error.response?.data?.detail || '修改位置失败');
          }
        }
      }
    });
  };

  const handleDeleteQueue = async (record) => {
    Modal.confirm({
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
          fetchQueue(selectedDeviceId, { notify: true, reason: 'leave' });
        } catch (error) {
          message.error('离开排队失败');
        }
      }
    });
  };

  const notifyMode = selectedDeviceId != null
    ? (notifyModes[String(selectedDeviceId)] || 'off')
    : 'off';

  const handleToggleNotifyMode = async () => {
    if (selectedDeviceId == null) return;
    const nextMode = notifyMode === 'off' ? 'once' : notifyMode === 'once' ? 'always' : 'off';
    if (nextMode !== 'off') {
      const permitted = await requestNotificationPermission();
      if (!permitted) {
        setNotifyModes(prev => ({
          ...prev,
          [String(selectedDeviceId)]: 'off'
        }));
        return;
      }
    }
    setNotifyModes(prev => ({
      ...prev,
      [String(selectedDeviceId)]: nextMode
    }));
  };

  const notifyLabel = notifyMode === 'once' ? '只提醒一次' : notifyMode === 'always' ? '一直提醒' : '';
  const notifyColor = notifyMode === 'off' ? '#bfbfbf' : '#1677ff';
  const notifyIcon = notifyMode === 'always'
    ? <ClockCircleFilled style={{ color: notifyColor }} />
    : <ClockCircleOutlined style={{ color: notifyColor }} />;

  const handleDropConfirm = (dragIndex, dropIndex) => {
    const dragRecord = queue[dragIndex];
    const dropRecord = queue[dropIndex];
    if (!dragRecord || !dropRecord) return;

    const newPosition = dropRecord.position;
    handleChangePosition(dragRecord, newPosition);
  };

  const queueColumns = [
    {
      title: '',
      dataIndex: 'drag',
      key: 'drag',
      width: 50,
      render: () => <HolderOutlined style={{ cursor: 'move', color: '#999' }} />
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
    { title: '检验员', dataIndex: 'inspector_name', key: 'inspector_name' },
    { title: '加入时间', dataIndex: 'submitted_at', key: 'submitted_at', render: formatTime },
    { title: 'ID', dataIndex: 'created_by_id', key: 'created_by_id', width: 90, align: 'center', render: renderUserId },
    {
      title: '操作',
      key: 'actions',
      width: 80,
      render: (_, record) => (
        <Button
          type="text"
          danger
          icon={<DeleteOutlined />}
          onClick={() => handleDeleteQueue(record)}
          disabled={record.position === 1}
        />
      )
    }
  ];

  return (
    <DndProvider backend={HTML5Backend}>
      <div>
      <Row gutter={[16, 16]}>
        {devices.map(device => {
          const config = statusConfig[device.status] || statusConfig.offline;
          const isSelected = device.id === selectedDeviceId;
          const isConfocal = isConfocalDevice(device);
          const olympus = device.metrics?.olympus || {};
          const imageProgress = Number.isFinite(Number(olympus.image_progress))
            ? Number(olympus.image_progress)
            : null;
          const frameCurrent = olympus.frame_current;
          const frameTotal = olympus.frame_total;
          const overallProgress = Number.isFinite(Number(device.task_progress))
            ? Number(device.task_progress)
            : 0;
          const groupTotal = olympus.group_total;
          const groupCompleted = olympus.group_completed;
          const frameLabel = frameCurrent
            ? `z${String(frameCurrent).padStart(3, '0')}/${frameTotal || '?'}`
            : '-';
          const xyPosition = olympus.xy_position;
          const zPosition = olympus.z_position;
          const zRange = olympus.z_range;
          const timeoutRemainingSeconds = getQueueTimeoutRemainingSeconds(device, nowTime);
          const showTimeoutCountdown = timeoutRemainingSeconds != null;
          const isTimeoutWarning = showTimeoutCountdown && timeoutRemainingSeconds <= 60;
          const extendedCount = device.queue_timeout_extended_count || 0;
          const remainingExtends = Math.max(0, 3 - extendedCount);
          const canExtend = remainingExtends > 0;
          return (
            <Col xs={24} sm={12} md={8} lg={6} key={device.id}>
              <Card 
                hoverable
                onClick={() => setSelectedDeviceId(device.id)}
                style={{ height: '100%', border: isSelected ? '2px solid #1677ff' : undefined }}
                title={
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span>{device.name}</span>
                    <Badge status={config.color} text={config.text} />
                  </div>
                }
                extra={<span style={{ fontSize: '12px', color: '#666' }}>排队: {device.queue_count || 0}</span>}
              >
                <div style={{ marginBottom: '12px' }}>
                  <Tag color="blue">型号: {device.model || '-'}</Tag>
                  <Tag color="green">位置: {device.location || '-'}</Tag>
                </div>
                
                {!isConfocal && Number.isFinite(Number(device.task_progress)) && (
                  <div style={{ marginBottom: '12px' }}>
                    <div style={{ fontSize: '14px', marginBottom: '4px' }}>
                      当前任务: {device.task_name || '未知任务'}
                    </div>
                    <Progress 
                      percent={Number(device.task_progress)} 
                      status="active"
                      strokeColor={{
                        '0%': '#108ee9',
                        '100%': '#87d068',
                      }}
                    />
                  </div>
                )}

                {isConfocal && (
                  <div style={{ marginBottom: '12px' }}>
                    <div style={{ fontSize: '14px', marginBottom: '4px' }}>
                      当前任务: {device.task_name || '未知任务'}
                    </div>
                    <div style={{ fontSize: 12, color: '#666', marginBottom: 6 }}>
                      设备状态: {getOlympusDisplayState(olympus, device.status)}
                    </div>
                    <Progress
                      percent={overallProgress}
                      status="active"
                      format={() => (
                        groupTotal ? `${overallProgress}% (${groupCompleted || 0}/${groupTotal})` : `${overallProgress}%`
                      )}
                    />
                    {olympus.current_file && (
                      <div style={{ fontSize: 12, color: '#666', marginTop: 6 }}>
                        当前文件: {olympus.current_file}
                      </div>
                    )}
                    {(() => {
                      const validOutputPath = getValidOutputPath(olympus.output_path);
                      return validOutputPath && (
                        <div
                          title={validOutputPath}
                          style={{ fontSize: 12, color: '#666', marginTop: 4, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                        >
                          输出路径: {validOutputPath}
                        </div>
                      );
                    })()}


                  </div>
                )}

                <div style={{ fontSize: '13px', color: '#666' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                    <span>心跳: {device.last_heartbeat ? formatRelativeTime(device.last_heartbeat) : '-'}</span>
                    {showTimeoutCountdown && (
                      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span
                          style={{
                            color: isTimeoutWarning ? '#cf1322' : '#fa8c16',
                            fontWeight: isTimeoutWarning ? 600 : 500,
                          }}
                        >
                          超时倒计时: {formatCountdown(timeoutRemainingSeconds)}
                        </span>
                        <Button
                          size="small"
                          type="link"
                          disabled={timeoutRemainingSeconds <= 0 || !canExtend}
                          onClick={(event) => {
                            event.stopPropagation();
                            handleExtendTimeout(device.id);
                          }}
                          title={canExtend ? `还能延长${remainingExtends}次` : '延长次数已达上限'}
                        >
                          延长5分钟{!canExtend ? '(已达上限)' : `(${remainingExtends})`}
                        </Button>
                      </span>
                    )}
                  </div>
                  <div>
                    {device.metrics?.temperature && <span>温度: {device.metrics.temperature}°C</span>}
                    {device.metrics?.temperature && device.task_elapsed_seconds != null && <span> | </span>}
                    {device.task_elapsed_seconds != null && (
                      <span>当前任务耗时: {Math.floor(device.task_elapsed_seconds / 60)}分钟</span>
                    )}
                  </div>

                </div>
              </Card>
            </Col>
          );
        })}
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 24 }}>
        <Col span={24}>
          <Card 
            title={selectedDevice ? `排队信息 - ${selectedDevice.name}` : '排队信息'}
            extra={selectedDevice && <span>当前排队人数：{queue.length}</span>}
          >
            {selectedDevice ? (
              <>
                <Form 
                  form={form}
                  layout="inline"
                  onFinish={handleJoinQueue}
                  onValuesChange={(_, values) => setInspectorName(values.inspector_name || '')}
                  style={{ marginBottom: 16 }}
                >
                  <Form.Item 
                    name="inspector_name" 
                    rules={[{ required: true, message: '请输入检验员姓名' }]}
                  >
                    <Input 
                      placeholder="检验员姓名" 
                      style={{ width: 150 }}
                    />
                  </Form.Item>
                  <Form.Item 
                    name="copies"
                    initialValue={1}
                  >
                    <InputNumber 
                      min={1} 
                      max={10}
                      placeholder="份数"
                      style={{ width: 80 }}
                    />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit" icon={<PlusOutlined />}>
                      加入排队
                    </Button>
                  </Form.Item>
                  <Form.Item style={{ marginRight: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <Button type="text" htmlType="button" onClick={handleToggleNotifyMode} style={{ padding: 0, height: 'auto' }}>
                        {notifyIcon}
                      </Button>
                      {notifyLabel && (
                        <span style={{ fontSize: 12, color: notifyColor }}>{notifyLabel}</span>
                      )}
                    </div>
                  </Form.Item>
                </Form>

                <Row gutter={[16, 16]}>
                  <Col xs={24} lg={24} xl={12} xxl={11}>
                    <DragTable 
                      dataSource={queue}
                      columns={queueColumns}
                      rowKey="id"
                      onDropConfirm={handleDropConfirm}
                    />
                  </Col>
                  <Col xs={24} lg={12} xl={7} xxl={8}>
                    <Card title="结果查看" size="small" style={{ height: '100%' }}>
                      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                        {!isConfocalDevice(selectedDevice) ? (
                          <Button
                            type="primary"
                            disabled={!selectedDevice || Number(selectedDevice.task_progress) !== 100}
                            onClick={() => setTableModal({ open: true, folder: null })}
                          >
                            查看表格
                          </Button>
                        ) : (
                          <Button
                            type="primary"
                            danger
                            disabled={!selectedDevice || Number(selectedDevice.task_progress) !== 100}
                            onClick={() => handleCleanupImages(null)}
                          >
                            删除多余图片
                          </Button>
                        )}
                        <Button
                          disabled={!selectedDevice || Number(selectedDevice.task_progress) !== 100}
                          onClick={() => setImagesModal({ open: true, folder: null })}
                        >
                          查看图片
                        </Button>
                      </div>
                      <div style={{ fontSize: 12, color: '#999', marginTop: 8 }}>
                        仅在检测完成（进度100%）后可查看结果
                      </div>
                      <div style={{ fontSize: 12, color: '#666', marginTop: 12 }}>
                        最近5次结果
                      </div>
                      <List
                        dataSource={recentResults}
                        locale={{ emptyText: '暂无历史结果' }}
                        size="small"
                        style={{ marginTop: 4 }}
                        renderItem={item => (
                          <List.Item style={{ paddingLeft: 0, paddingRight: 0 }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%' }}>
                              <div style={{ flex: 1, minWidth: 0, fontSize: 13, color: '#333' }}>
                                <div style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                  {item.task_name || item.folder || '-'}
                                </div>
                              </div>
                              <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
                                {!isConfocalDevice(selectedDevice) ? (
                                  <Button
                                    size="small"
                                    disabled={!item.folder}
                                    onClick={() => setTableModal({ open: true, folder: item.folder })}
                                  >
                                    查看表格
                                  </Button>
                                ) : (
                                  <Button
                                    size="small"
                                    danger
                                    disabled={!item.folder}
                                    onClick={() => handleCleanupImages(item.folder)}
                                  >
                                    删除多余图片
                                  </Button>
                                )}
                                <Button
                                  size="small"
                                  disabled={!item.folder}
                                  onClick={() => setImagesModal({ open: true, folder: item.folder })}
                                >
                                  查看图片
                                </Button>
                              </div>
                            </div>
                          </List.Item>
                        )}
                      />
                      {!isConfocalDevice(selectedDevice) && (
                        <ResultsModal
                          open={tableModal.open}
                          title="结果表格"
                          url={buildResultsUrl('table', tableModal.folder)}
                          onClose={() => setTableModal({ open: false, folder: null })}
                        />
                      )}
                      <Modal
                        title="结果图片"
                        open={imagesModal.open}
                        onCancel={() => setImagesModal({ open: false, folder: null })}
                        footer={null}
                        width="90vw"
                        style={{ top: 20 }}
                        bodyStyle={{ height: '80vh', padding: 0, width: '100%', overflow: 'hidden' }}
                        destroyOnClose
                      >
                        {imagesModal.open && (
                          <ResultsImages
                            deviceId={selectedDeviceId}
                            folder={imagesModal.folder}
                            embedded
                            clientBaseUrl={selectedDevice?.client_base_url || null}
                          />
                        )}
                      </Modal>
                    </Card>
                  </Col>
                  <Col xs={24} lg={12} xl={5} xxl={5}>
                    <Card title="历史记录（今日）" size="small" style={{ height: '100%' }}>
                      <List
                        dataSource={queueLogs}
                        style={{ maxHeight: 240, overflowY: 'auto' }}
                        renderItem={log => {
                          const isCompletionLog = log.new_position === 0;
                          const isLeaveLog = log.new_position === -1;
                          const isTimeoutShiftLog = log.change_type === 'timeout_shift';
                          const isTimeoutExtendLog = log.change_type === 'timeout_extend';
                          return (
                            <List.Item>
                              <div style={{ width: '100%' }}>
                                <div style={{ fontSize: '12px', color: '#999' }}>
                                  {formatDateTime(log.change_time)} - {renderUserLabel(log.changed_by, log.changed_by_id)}
                                </div>
                                {isTimeoutShiftLog ? (
                                  <div style={{ color: '#ff4d4f', fontWeight: 600 }}>
                                    {log.remark || '超时未使用设备，已顺延'}
                                  </div>
                                ) : isTimeoutExtendLog ? (
                                  <div style={{ color: '#fa8c16', fontWeight: 600 }}>
                                    {log.remark || '设备超时已延长'}
                                  </div>
                                ) : isCompletionLog ? (
                                  <div style={{ color: '#52c41a', fontWeight: 600 }}>测量完成</div>
                                ) : isLeaveLog ? (
                                  <div style={{ color: '#ff4d4f', fontWeight: 600 }}>离开排队</div>
                                ) : (
                                  <div>
                                    {getQueuePositionLabel(log.old_position)} → {getQueuePositionLabel(log.new_position)}
                                  </div>
                                )}
                              </div>
                            </List.Item>
                          );
                        }}
                      />
                    </Card>
                  </Col>
                </Row>

              </>
            ) : (
              <div style={{ color: '#999' }}>请选择设备查看排队信息</div>
            )}
          </Card>
        </Col>
      </Row>
      </div>
    </DndProvider>
  );
}

export default DeviceMonitor;
