import { useState, useEffect } from 'react';
import { Card, Form, DatePicker, Select, Button, Table, Input, message } from 'antd';
import { SearchOutlined, DownloadOutlined } from '@ant-design/icons';
import { historyApi } from '../api/history';
import { deviceApi } from '../api/devices';
import { formatDateTime } from '../utils/dateHelper';
import dayjs from 'dayjs';
import { saveAs } from 'file-saver';

const { RangePicker } = DatePicker;

function HistoryQuery() {
  const [devices, setDevices] = useState([]);
  const [data, setData] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState({});
  const [pagination, setPagination] = useState({ page: 1, pageSize: 20 });

  const fetchDevices = async () => {
    try {
      const data = await deviceApi.getAll();
      setDevices(data);
    } catch (error) {
      message.error('获取设备列表失败');
    }
  };

  const fetchHistory = async () => {
    setLoading(true);
    try {
      const params = {
        page: pagination.page,
        page_size: pagination.pageSize,
        ...filters
      };
      const response = await historyApi.get(params);
      setData(response.data);
      setTotal(response.total);
    } catch (error) {
      message.error('获取历史记录失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDevices();
    fetchHistory();
  }, [pagination, filters]);

  const handleSearch = (values) => {
    const newFilters = {};
    if (values.device_id) newFilters.device_id = values.device_id;
    if (values.status) newFilters.status = values.status;
    if (values.task_id) newFilters.task_id = values.task_id;
    if (values.dateRange) {
      newFilters.start_date = values.dateRange[0].toISOString();
      newFilters.end_date = values.dateRange[1].toISOString();
    }
    setFilters(newFilters);
    setPagination({ ...pagination, page: 1 });
  };

  const handleExport = async () => {
    try {
      const blob = await historyApi.export(filters);
      saveAs(blob, `device_history_${dayjs().format('YYYYMMDDHHmmss')}.xlsx`);
      message.success('导出成功');
    } catch (error) {
      message.error('导出失败');
    }
  };

  const columns = [
    { title: '时间', dataIndex: 'reported_at', render: formatDateTime, width: 180 },
    { title: '设备', dataIndex: 'device_id', width: 80 },
    { title: '状态', dataIndex: 'status', width: 100 },
    { title: '任务ID', dataIndex: 'task_id', width: 150 },
    { title: '任务名称', dataIndex: 'task_name', width: 200 },
    { title: '进度', dataIndex: 'task_progress', width: 80, render: (v) => v ? `${v}%` : '-' },
    { title: '设备指标', dataIndex: 'device_metrics', render: (v) => v ? JSON.stringify(v) : '-' },
  ];

  return (
    <div>
      <Card style={{ marginBottom: 16 }}>
        <Form layout="inline" onFinish={handleSearch}>
          <Form.Item name="device_id" label="设备">
            <Select 
              placeholder="全部" 
              style={{ width: 150 }}
              allowClear
              options={devices.map(d => ({ label: d.name, value: d.id }))}
            />
          </Form.Item>
          <Form.Item name="status" label="状态">
            <Select 
              placeholder="全部" 
              style={{ width: 120 }}
              allowClear
              options={[
                { label: '空闲', value: 'idle' },
                { label: '检测中', value: 'busy' },
                { label: '维护', value: 'maintenance' },
                { label: '故障', value: 'error' },
              ]}
            />
          </Form.Item>
          <Form.Item name="task_id" label="任务ID">
            <Input placeholder="任务ID" style={{ width: 150 }} />
          </Form.Item>
          <Form.Item name="dateRange" label="日期范围">
            <RangePicker style={{ width: 300 }} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" icon={<SearchOutlined />}>
              查询
            </Button>
          </Form.Item>
          <Form.Item>
            <Button onClick={handleExport} icon={<DownloadOutlined />}>
              导出Excel
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Card>
        <Table
          dataSource={data}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={{
            current: pagination.page,
            pageSize: pagination.pageSize,
            total,
            onChange: (page, pageSize) => setPagination({ page, pageSize }),
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 条记录`,
          }}
        />
      </Card>
    </div>
  );
}

export default HistoryQuery;
