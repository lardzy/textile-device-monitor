import { useState, useEffect } from 'react';
import { Card, Table, Button, Modal, Form, Input, message, Popconfirm } from 'antd';
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons';
import { deviceApi } from '../api/devices';

function DeviceManagement() {
  const [devices, setDevices] = useState([]);
  const [modalVisible, setModalVisible] = useState(false);
  const [editingDevice, setEditingDevice] = useState(null);
  const [form] = Form.useForm();

  const fetchDevices = async () => {
    try {
      const data = await deviceApi.getAll();
      setDevices(data);
    } catch (error) {
      message.error('获取设备列表失败');
    }
  };

  useEffect(() => {
    fetchDevices();
  }, []);

  const handleAdd = () => {
    setEditingDevice(null);
    form.resetFields();
    setModalVisible(true);
  };

  const handleEdit = (device) => {
    setEditingDevice(device);
    form.setFieldsValue(device);
    setModalVisible(true);
  };

  const handleDelete = async (id) => {
    try {
      await deviceApi.delete(id);
      message.success('删除成功');
      fetchDevices();
    } catch (error) {
      message.error('删除失败');
    }
  };

  const handleSubmit = async (values) => {
    try {
      if (editingDevice) {
        await deviceApi.update(editingDevice.id, values);
        message.success('更新成功');
      } else {
        await deviceApi.create(values);
        message.success('创建成功');
      }
      setModalVisible(false);
      fetchDevices();
    } catch (error) {
      message.error(editingDevice ? '更新失败' : '创建失败');
    }
  };

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 80 },
    { title: '设备编码', dataIndex: 'device_code', width: 120 },
    { title: '设备名称', dataIndex: 'name' },
    { title: '型号', dataIndex: 'model' },
    { title: '位置', dataIndex: 'location' },
    { title: '状态', dataIndex: 'status' },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, record) => (
        <div>
          <Button type="link" icon={<EditOutlined />} onClick={() => handleEdit(record)}>
            编辑
          </Button>
          <Popconfirm
            title="确认删除"
            description="确定要删除该设备吗？"
            onConfirm={() => handleDelete(record.id)}
            okText="确定"
            cancelText="取消"
          >
            <Button type="link" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </div>
      )
    }
  ];

  return (
    <div>
      <Card
        title="设备管理"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>
          添加设备
        </Button>}
      >
        <Table
          dataSource={devices}
          columns={columns}
          rowKey="id"
          pagination={{ pageSize: 10 }}
        />
      </Card>

      <Modal
        title={editingDevice ? '编辑设备' : '添加设备'}
        open={modalVisible}
        onCancel={() => setModalVisible(false)}
        onOk={() => form.submit()}
        width={600}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
        >
          <Form.Item
            name="device_code"
            label="设备编码"
            rules={[{ required: true, message: '请输入设备编码' }]}
          >
            <Input placeholder="设备编码（唯一）" />
          </Form.Item>
          <Form.Item
            name="name"
            label="设备名称"
            rules={[{ required: true, message: '请输入设备名称' }]}
          >
            <Input placeholder="设备名称" />
          </Form.Item>
          <Form.Item name="model" label="设备型号">
            <Input placeholder="设备型号" />
          </Form.Item>
          <Form.Item name="location" label="设备位置">
            <Input placeholder="设备位置" />
          </Form.Item>
          <Form.Item name="description" label="设备描述">
            <Input.TextArea rows={4} placeholder="设备描述" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export default DeviceManagement;
