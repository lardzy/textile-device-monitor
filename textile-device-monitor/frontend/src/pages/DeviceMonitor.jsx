import { useState, useEffect, useMemo, useRef } from 'react';
import { Card, Row, Col, Badge, Tag, Progress, Form, Input, Button, Table, List, Select, message, Modal } from 'antd';
import { CheckCircleOutlined, ClockCircleOutlined, ClockCircleFilled, ExclamationCircleOutlined, LoadingOutlined, StopOutlined, PlusOutlined, ArrowUpOutlined, ArrowDownOutlined, DeleteOutlined } from '@ant-design/icons';
import { deviceApi } from '../api/devices';
import { queueApi } from '../api/queue';
import { resultsApi } from '../api/results';
import ResultsModal from '../components/ResultsModal';
import wsClient from '../websocket/client';
import { formatRelativeTime, formatDateTime, formatTime } from '../utils/dateHelper';
import { addQueueNoticeEntry, getInspectorName, getQueueNoticeEntries, getQueueNoticeModes, removeQueueNoticeEntry, saveInspectorName, saveQueueNoticeModes } from '../utils/localStorage';


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

const isConfocalDevice = (device) => {
  if (!device) return false;
  return device.metrics?.device_type === 'laser_confocal' || Boolean(device.metrics?.olympus);
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
  const [form] = Form.useForm();
  const devicesRef = useRef([]);
  const notifyModesRef = useRef(notifyModes);
  const lastProgressRef = useRef(new Map());
  const lastQueueCompletionRef = useRef(new Map());
  const lastDeviceNotificationRef = useRef(new Map());
  const deviceNotifyTimersRef = useRef(new Map());


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

  const fetchQueue = async (deviceId) => {
    if (!deviceId) return;
    try {
      const data = await queueApi.getByDevice(deviceId);
      const sortedQueue = (data.queue || [])
        .slice()
        .sort((a, b) => a.position - b.position)
        .map((item, index) => ({ ...item, position: index + 1 }));
      const sortedLogs = (data.logs || []).slice().sort((a, b) => new Date(b.change_time) - new Date(a.change_time));
      setQueue(sortedQueue);
      setQueueLogs(sortedLogs);
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

  const getNotifyModeByDevice = (deviceId) => {
    if (deviceId == null) return 'off';
    const key = String(deviceId);
    return notifyModesRef.current[key] || 'off';
  };

  const handleQueueCompletion = async (data) => {
    if (!data) return;
    const queueId = data.queue_id != null ? Number(data.queue_id) : null;
    const entries = getQueueNoticeEntries();
    const entry = entries.find(item => queueId != null && item.id === queueId)
      || entries.find(item => item.id === data.queue_id)
      || entries.find(item => item.device_id === data.device_id && item.inspector_name === data.completed_by);
    if (!entry) {
      return;
    }
    removeQueueNoticeEntry(entry.id);
    const permitted = await requestNotificationPermission();
    if (!permitted) {
      return;
    }
    if (entry.device_id != null) {
      const lastDeviceTime = lastDeviceNotificationRef.current.get(entry.device_id);
      if (lastDeviceTime && Date.now() - lastDeviceTime < 2000) {
        return;
      }
    }
    const deviceName = data.device_name
      || devicesRef.current.find(device => device.id === entry.device_id)?.name
      || '';
    sendQueueNotification(entry, deviceName);
    const deviceId = entry.device_id;
    if (deviceId != null) {
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
        handleQueueCompletion(data);
      }
      if (data.action === 'leave' && data.queue_id) {
        removeQueueNoticeEntry(data.queue_id);
      }
      if (data.device_id === selectedDeviceId) {
        fetchQueue(selectedDeviceId);
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
        const device = devicesRef.current.find(item => item.id === selectedDeviceId);
        fetchRecentResults(device);
      }
    }, 8000);

    return () => {
      wsClient.off('device_status_update');
      wsClient.off('device_list_update');
      wsClient.off('device_offline');
      wsClient.off('queue_update');
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
      const queueRecord = await queueApi.join({
        inspector_name: values.inspector_name,
        device_id: selectedDeviceId
      });
      message.success('加入排队成功');
      saveInspectorName(values.inspector_name);
      if (queueRecord?.id) {
        addQueueNoticeEntry({
          id: queueRecord.id,
          device_id: queueRecord.device_id,
          inspector_name: queueRecord.inspector_name,
        });
      }
      form.resetFields();
      setInspectorName('');
      fetchQueue(selectedDeviceId);
    } catch (error) {
      message.error('加入排队失败');
    }
  };

  const handleChangePosition = async (queueId, newPosition) => {
    try {
      const changedBy = inspectorName?.trim() || '系统';
      await queueApi.updatePosition(queueId, {
        new_position: newPosition,
        changed_by: changedBy
      });
      message.success('修改位置成功');
      fetchQueue(selectedDeviceId);
    } catch (error) {
      message.error('修改位置失败');
    }
  };

  const handleDeleteQueue = async (queueId) => {
    try {
      await queueApi.leave(queueId);
      message.success('离开排队成功');
      removeQueueNoticeEntry(queueId);
      fetchQueue(selectedDeviceId);
    } catch (error) {
      message.error('离开排队失败');
    }
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

  const queueColumns = [
    { title: '位置', dataIndex: 'position', key: 'position', width: 80 },
    { title: '检验员', dataIndex: 'inspector_name', key: 'inspector_name' },
    { title: '加入时间', dataIndex: 'submitted_at', key: 'submitted_at', render: formatTime },
    {
      title: '操作',
      key: 'actions',
      width: 140,
      render: (_, record, index) => (
        <div>
          <Button 
            type="text" 
            icon={<ArrowUpOutlined />} 
            onClick={() => handleChangePosition(record.id, record.position - 1)}
            disabled={index === 0}
          />
          <Button 
            type="text" 
            icon={<ArrowDownOutlined />} 
            onClick={() => handleChangePosition(record.id, record.position + 1)}
            disabled={index === queue.length - 1}
          />
          <Button 
            type="text" 
            danger 
            icon={<DeleteOutlined />} 
            onClick={() => handleDeleteQueue(record.id)}
          />
        </div>
      )
    }
  ];

  return (
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
                    <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}>
                      当前图片进度: {frameLabel}
                    </div>
                    <Progress
                      percent={imageProgress ?? 0}
                      status="active"
                      format={() => (imageProgress != null ? `${imageProgress}%` : frameLabel)}
                    />
                    <div style={{ fontSize: 12, color: '#666', marginTop: 8, marginBottom: 4 }}>
                      多点总进度
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
                    {olympus.output_path && (
                      <div
                        title={olympus.output_path}
                        style={{ fontSize: 12, color: '#666', marginTop: 4, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                      >
                        输出路径: {olympus.output_path}
                      </div>
                    )}

                    {(xyPosition || zPosition != null) && (
                      <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
                        {xyPosition ? `样品台: ${xyPosition.x}, ${xyPosition.y}` : ''}
                        {xyPosition && zPosition != null ? ' | ' : ''}
                        {zPosition != null ? `Z: ${zPosition}` : ''}
                      </div>
                    )}
                    {zRange && (
                      <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
                        Z范围: {zRange.start} ~ {zRange.end}
                      </div>
                    )}
                  </div>
                )}

                <div style={{ fontSize: '13px', color: '#666' }}>
                  <div>心跳: {device.last_heartbeat ? formatRelativeTime(device.last_heartbeat) : '-'}</div>
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
                    <Table 
                      dataSource={queue}
                      columns={queueColumns}
                      rowKey="id"
                      pagination={false}
                      size="small"
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
                      <ResultsModal
                        open={imagesModal.open}
                        title="结果图片"
                        url={buildResultsUrl('images', imagesModal.folder)}
                        onClose={() => setImagesModal({ open: false, folder: null })}
                      />
                    </Card>
                  </Col>
                  <Col xs={24} lg={12} xl={5} xxl={5}>
                    <Card title="修改历史（今日）" size="small" style={{ height: '100%' }}>
                      <List
                        dataSource={queueLogs}
                        style={{ maxHeight: 240, overflowY: 'auto' }}
                        renderItem={log => (
                          <List.Item>
                            <div style={{ width: '100%' }}>
                              <div style={{ fontSize: '12px', color: '#999' }}>
                                {formatDateTime(log.change_time)} - {log.changed_by}
                              </div>
                              <div>
                                位置 {log.old_position} → {log.new_position}
                              </div>
                            </div>
                          </List.Item>
                        )}
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
  );
}

export default DeviceMonitor;
