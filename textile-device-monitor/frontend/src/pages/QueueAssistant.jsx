import { useState, useEffect } from 'react';
import { Card, Form, Input, Button, Table, List, Select, message, Modal, Row, Col } from 'antd';
import { PlusOutlined, ArrowUpOutlined, ArrowDownOutlined, DeleteOutlined } from '@ant-design/icons';
import { queueApi } from '../api/queue';
import { deviceApi } from '../api/devices';
import wsClient from '../websocket/client';
import { addQueueNoticeEntry, getInspectorName, removeQueueNoticeEntry, saveInspectorName } from '../utils/localStorage';
import { formatDateTime, formatTime } from '../utils/dateHelper';

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

function QueueAssistant() {
  const [devices, setDevices] = useState([]);
  const [selectedDevice, setSelectedDevice] = useState(null);
  const [queue, setQueue] = useState([]);
  const [logs, setLogs] = useState([]);
  const [inspectorName, setInspectorName] = useState(getInspectorName());
  const [form] = Form.useForm();

  const fetchDevices = async () => {
    try {
      const data = await deviceApi.getAll();
      setDevices(data);
    } catch (error) {
      message.error('获取设备列表失败');
    }
  };

  const fetchQueue = async (deviceId) => {
    if (!deviceId) return;
    try {
      const data = await queueApi.getByDevice(deviceId);
      setQueue(data.queue || []);
      setLogs(data.logs || []);
    } catch (error) {
      message.error('获取排队列表失败');
    }
  };

  useEffect(() => {
    fetchDevices();

    wsClient.on('queue_update', (data) => {
      if (data.device_id === selectedDevice) {
        fetchQueue(selectedDevice);
      }
    });

    return () => {
      wsClient.off('queue_update');
    };
  }, [selectedDevice]);

  const handleJoinQueue = async (values) => {
    try {
      const queueRecord = await queueApi.join({
        inspector_name: values.inspector_name,
        device_id: values.device_id
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
      fetchQueue(values.device_id);
      form.resetFields();
    } catch (error) {
      message.error('加入排队失败');
    }
  };

  const handleChangePosition = async (queueId, newPosition) => {
    try {
      await queueApi.updatePosition(queueId, {
        new_position: newPosition,
        changed_by: inspectorName
      });
      message.success('修改位置成功');
      fetchQueue(selectedDevice);
    } catch (error) {
      message.error('修改位置失败');
    }
  };

  const handleDeleteQueue = async (queueId) => {
    Modal.confirm({
      title: '确认删除',
      content: '确定要离开排队吗？',
      onOk: async () => {
        try {
          await queueApi.leave(queueId);
          message.success('离开排队成功');
          removeQueueNoticeEntry(queueId);
          fetchQueue(selectedDevice);
        } catch (error) {
          message.error('离开排队失败');
        }
      }
    });
  };

  const queueColumns = [
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
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <div>
          <Button 
            type="text" 
            icon={<ArrowUpOutlined />} 
            onClick={() => handleChangePosition(record.id, Math.max(1, record.position - 1))}
            disabled={record.position <= 1}
          />
          <Button 
            type="text" 
            icon={<ArrowDownOutlined />} 
            onClick={() => handleChangePosition(record.id, record.position + 1)}
            disabled={record.position >= queue.length}
          />
          <Button 
            type="text" 
            danger 
            icon={<DeleteOutlined />} 
            onClick={() => handleDeleteQueue(record.id)}
            disabled={record.position === 1}
          />
        </div>
      )
    }
  ];

  return (
    <div>
      <Row gutter={[16, 16]}>
        <Col span={24}>
          <Card title="加入排队">
              <Form 
                form={form}
                layout="inline"
                onFinish={handleJoinQueue}
                initialValues={{ inspector_name: inspectorName }}
              >

              <Form.Item 
                name="inspector_name" 
                rules={[{ required: true, message: '请输入检验员姓名' }]}
              >
                <Input 
                  placeholder="检验员姓名" 
                  style={{ width: 150 }}
                  onChange={(e) => setInspectorName(e.target.value)}
                />
              </Form.Item>
              <Form.Item 
                name="device_id" 
                rules={[{ required: true, message: '请选择设备' }]}
              >
                <Select 
                  placeholder="选择设备" 
                  style={{ width: 200 }}
                  options={devices.map(d => ({ label: d.name, value: d.id }))}
                />
              </Form.Item>
              <Form.Item>
                <Button type="primary" htmlType="submit" icon={<PlusOutlined />}>
                  加入排队
                </Button>
              </Form.Item>
            </Form>
          </Card>
        </Col>
        <Col span={16}>
          <Card 
            title="排队列表"
            extra={
              <Select 
                placeholder="选择设备" 
                style={{ width: 200 }}
                value={selectedDevice}
                onChange={(value) => {
                  setSelectedDevice(value);
                  fetchQueue(value);
                }}
                options={devices.map(d => ({ label: d.name, value: d.id }))}
              />
            }
          >
            <Table 
              dataSource={queue}
              columns={queueColumns}
              rowKey="id"
              pagination={false}
              size="small"
              onRow={(record) => ({
                style: record.position === 1 ? { background: '#f6ffed' } : undefined
              })}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card title="历史记录（今日）" style={{ height: '100%' }}>
            <List
              dataSource={logs}
              renderItem={log => {
                const isCompletionLog = log.new_position === 0;
                return (
                  <List.Item>
                    <div style={{ width: '100%' }}>
                      <div style={{ fontSize: '12px', color: '#999' }}>
                        {formatDateTime(log.change_time)} - {log.changed_by}
                      </div>
                      {isCompletionLog ? (
                        <div style={{ color: '#52c41a', fontWeight: 600 }}>测量完成</div>
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
    </div>
  );
}

export default QueueAssistant;
