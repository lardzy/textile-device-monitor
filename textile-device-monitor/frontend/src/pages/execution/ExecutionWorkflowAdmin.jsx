import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  createExecutionWorkflow,
  getExecutionCategories,
  getExecutionWorkflows,
} from '../../api/execution';
import { createDefaultDefinition } from '../../utils/executionWorkflow';
import ExecutionChrome from './ExecutionChrome';
import './execution.css';

const { Text } = Typography;

export default function ExecutionWorkflowAdmin() {
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const [workflows, setWorkflows] = useState([]);
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [workflowRows, categoryRows] = await Promise.all([
        getExecutionWorkflows({ include_drafts: true }),
        getExecutionCategories(),
      ]);
      setWorkflows(workflowRows);
      setCategories(categoryRows);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const categoryOptions = useMemo(() => categories.map(category => ({
    value: category.id || category.slug || category.name,
    label: category.name,
  })), [categories]);

  const createWorkflow = async () => {
    try {
      const values = await form.validateFields();
      setCreating(true);
      const slug = `workflow-${Date.now().toString(36)}`;
      const payload = await createExecutionWorkflow({
        ...values,
        slug,
        definition: createDefaultDefinition({
          name: values.name,
          category: values.category_id,
        }),
        capabilities: { read: true, write: false },
        is_enabled: true,
      });
      const workflow = payload?.workflow || payload;
      const id = workflow?.id || workflow?.workflow_id;
      message.success('流程草稿已创建');
      setCreateOpen(false);
      form.resetFields();
      if (id) {
        navigate(`/execution/admin/workflows/${id}`);
      } else {
        load();
      }
    } catch (requestError) {
      if (requestError?.errorFields) {
        return;
      }
      message.error(requestError.message || '创建流程失败');
    } finally {
      setCreating(false);
    }
  };

  const columns = [
    {
      title: '流程',
      dataIndex: 'name',
      render: (value, row) => (
        <div className="execution-admin-workflow-name">
          <Text strong>{value || row.title}</Text>
          <Text type="secondary">{row.description || '暂无说明'}</Text>
        </div>
      ),
    },
    {
      title: '类别',
      dataIndex: 'category',
      width: 140,
      render: (value, row) => (
        <Tag color="geekblue">
          {typeof value === 'object' ? value.name : row.category_name || value || '未分类'}
        </Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 130,
      render: (value, row) => row.published_version
        ? <Tag color="success">已发布 v{row.published_version.version || row.published_version}</Tag>
        : <Tag color="warning">{value === 'archived' ? '已归档' : '仅草稿'}</Tag>,
    },
    {
      title: '草稿版本',
      dataIndex: 'draft_revision',
      width: 100,
      render: value => value ?? '—',
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 170,
      render: value => value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '—',
    },
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, row) => (
        <Button
          type="link"
          icon={<EditOutlined />}
          onClick={() => navigate(`/execution/admin/workflows/${row.id || row.workflow_id}`)}
        >
          设计
        </Button>
      ),
    },
  ];

  return (
    <div className="execution-page execution-admin-page">
      <ExecutionChrome
        title="流程管理"
        subtitle="设计、验证和发布可复用的检测执行流程"
        backTo={{ path: '/execution', label: '流程目录' }}
        actions={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              新建流程
            </Button>
          </Space>
        )}
      />
      {error && (
        <Alert
          showIcon
          type="error"
          message="流程列表加载失败"
          description={error.message}
          action={<Button onClick={load}>重试</Button>}
        />
      )}
      <div className="execution-admin-table">
        <Table
          rowKey={row => row.id || row.workflow_id}
          loading={loading}
          columns={columns}
          dataSource={workflows}
          pagination={{ pageSize: 12, showSizeChanger: false }}
        />
      </div>
      <Modal
        title="新建流程"
        open={createOpen}
        okText="创建并开始设计"
        cancelText="取消"
        confirmLoading={creating}
        onOk={createWorkflow}
        onCancel={() => setCreateOpen(false)}
        destroyOnClose
      >
        <Form form={form} layout="vertical" requiredMark="optional">
          <Form.Item name="name" label="流程名称" rules={[{ required: true, message: '请输入流程名称' }]}>
            <Input placeholder="例如：棉与再生纤维素定量分析" />
          </Form.Item>
          <Form.Item name="category_id" label="所属类别" rules={[{ required: true, message: '请选择类别' }]}>
            <Select options={categoryOptions} placeholder="选择大类" />
          </Form.Item>
          <Form.Item name="description" label="流程说明">
            <Input.TextArea rows={3} placeholder="说明适用场景和主要产出" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
