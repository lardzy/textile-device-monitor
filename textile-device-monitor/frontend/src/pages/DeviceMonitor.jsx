import { useState, useEffect, useMemo } from 'react';
import { Card, Row, Col, Badge, Tag, Progress, Form, Input, Button, Table, List, Select, message } from 'antd';
import { CheckCircleOutlined, ClockCircleOutlined, ExclamationCircleOutlined, LoadingOutlined, StopOutlined, PlusOutlined, ArrowUpOutlined, ArrowDownOutlined, DeleteOutlined } from '@ant-design/icons';
import { deviceApi } from '../api/devices';
import { queueApi } from '../api/queue';
import { resultsApi } from '../api/results';
import ResultsModal from '../components/ResultsModal';
import wsClient from '../websocket/client';
import { formatRelativeTime, formatDateTime, formatTime } from '../utils/dateHelper';
import { getInspectorName, saveInspectorName } from '../utils/localStorage';


const statusConfig = {
  idle: { color: 'success', icon: <CheckCircleOutlined />, text: '空闲' },
  busy: { color: 'processing', icon: <LoadingOutlined />, text: '检测中' },
  maintenance: { color: 'warning', icon: <ClockCircleOutlined />, text: '维护中' },
  error: { color: 'error', icon: <ExclamationCircleOutlined />, text: '故障' },
  offline: { color: 'default', icon: <StopOutlined />, text: '离线' },
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
  const [form] = Form.useForm();


  const selectedDevice = useMemo(() => {
    return devices.find(device => device.id === selectedDeviceId) || null;
  }, [devices, selectedDeviceId]);

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

  const fetchRecentResults = async (deviceId) => {
    if (!deviceId) {
      setRecentResults([]);
      return;
    }
    try {
      const data = await resultsApi.getRecent(deviceId, 5);
      setRecentResults(data.items || []);
    } catch (error) {
      console.error('Failed to fetch recent results:', error);
    }
  };

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
        fetchRecentResults(selectedDeviceId);
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
    if (selectedDeviceId) {
      fetchQueue(selectedDeviceId);
      fetchRecentResults(selectedDeviceId);
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


  const handleJoinQueue = async (values) => {
    try {
      await queueApi.join({
        inspector_name: values.inspector_name,
        device_id: selectedDeviceId
      });
      message.success('加入排队成功');
      saveInspectorName(values.inspector_name);
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
      fetchQueue(selectedDeviceId);
    } catch (error) {
      message.error('离开排队失败');
    }
  };

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
                
                {Number.isFinite(Number(device.task_progress)) && (
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
                        <Button
                          type="primary"
                          disabled={!selectedDevice || Number(selectedDevice.task_progress) !== 100}
                          onClick={() => setTableModal({ open: true, folder: null })}
                        >
                          查看表格
                        </Button>
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
                                <Button
                                  size="small"
                                  disabled={!item.folder}
                                  onClick={() => setTableModal({ open: true, folder: item.folder })}
                                >
                                  查看表格
                                </Button>
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
                      <ResultsModal
                        open={tableModal.open}
                        title="结果表格"
                        url={buildResultsUrl('table', tableModal.folder)}
                        onClose={() => setTableModal({ open: false, folder: null })}
                      />
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
