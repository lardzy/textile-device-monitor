import { useState, useEffect } from 'react';
import { Card, Row, Col, Select, DatePicker, Space } from 'antd';
import { BarChart, Bar, PieChart, Pie, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from 'recharts';
import { statsApi } from '../api/stats';
import dayjs from 'dayjs';

const { RangePicker } = DatePicker;

function Statistics() {
  const [stats, setStats] = useState({});
  const [summary, setSummary] = useState([]);
  const [dateRange, setDateRange] = useState([dayjs().subtract(7, 'day'), dayjs()]);
  const [statType, setStatType] = useState('daily');

  useEffect(() => {
    fetchStats();
    fetchSummary();
  }, [dateRange, statType]);

  const fetchStats = async () => {
    try {
      const data = await statsApi.getRealtime();
      setStats(data);
    } catch (error) {
      console.error('Failed to fetch stats');
    }
  };

  const fetchSummary = async () => {
    if (!dateRange || !dateRange[0] || !dateRange[1]) {
      setSummary([]);
      return;
    }

    try {
      const data = await statsApi.getSummary({
        stat_type: statType,
        start_date: dateRange[0].format('YYYY-MM-DD'),
        end_date: dateRange[1].format('YYYY-MM-DD')
      });
      const normalized = (Array.isArray(data) ? data : []).map((item) => {
        const utilizationRate = Number(item?.utilization_rate);
        const utilizationFallback = Number(item?.utilization);
        const resolvedUtilization = Number.isFinite(utilizationRate)
          ? utilizationRate
          : Number.isFinite(utilizationFallback)
            ? utilizationFallback
            : 0;
        return {
          ...item,
          device_name: item?.device_name || (item?.device_id ? `设备${item.device_id}` : '-'),
          utilization_rate: resolvedUtilization,
        };
      });
      setSummary(normalized);
    } catch (error) {
      console.error('Failed to fetch summary');
    }
  };

  const pieData = [
    { name: '在线', value: stats.online_devices || 0 },
    { name: '离线', value: stats.offline_devices || 0 },
  ];

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={24}>
          <Card>
            <Space direction="horizontal" style={{ width: '100%', justifyContent: 'space-between' }}>
              <div>
                <RangePicker 
                  value={dateRange}
                  onChange={(value) => setDateRange(value || [])}
                  allowClear
                />
              </div>
              <Select 
                value={statType}
                onChange={setStatType}
                style={{ width: 120 }}
                options={[
                  { label: '日统计', value: 'daily' },
                  { label: '周统计', value: 'weekly' },
                  { label: '月统计', value: 'monthly' }
                ]}
              />
            </Space>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col span={24}>
          <Card title="设备状态分布">
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  labelLine={false}
                  label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
                  outerRadius={80}
                  fill="#8884d8"
                  dataKey="value"
                >
                  {pieData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </Card>
        </Col>

        <Col span={12}>
          <Card title="任务完成量">
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={summary}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis
                  dataKey="device_name"
                  interval={0}
                  angle={-30}
                  textAnchor="end"
                  height={70}
                />
                <YAxis />
                <Tooltip />
                <Legend />
                <Bar dataKey="total_tasks" fill="#8884d8" name="总任务数" />
                <Bar dataKey="completed_tasks" fill="#82ca9d" name="完成任务数" />
              </BarChart>
            </ResponsiveContainer>
          </Card>
        </Col>

        <Col span={12}>
          <Card title="设备利用率">
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={summary}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis
                  dataKey="device_name"
                  interval={0}
                  angle={-30}
                  textAnchor="end"
                  height={70}
                />
                <YAxis domain={[0, 100]} />
                <Tooltip formatter={(value) => {
                  const numeric = Number(value);
                  return `${Number.isFinite(numeric) ? numeric.toFixed(2) : '0.00'}%`;
                }} />
                <Bar dataKey="utilization_rate" fill="#ffc658" name="利用率" />
              </BarChart>
            </ResponsiveContainer>
          </Card>
        </Col>
      </Row>
    </div>
  );
}

const COLORS = ['#0088FE', '#00C49F', '#FFBB28', '#FF8042'];

export default Statistics;
