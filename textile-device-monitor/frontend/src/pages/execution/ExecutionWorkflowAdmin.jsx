import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Button,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import {
  EditOutlined,
  FileTextOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  getExecutionWorkflows,
} from '../../api/execution';
import ExecutionChrome from './ExecutionChrome';
import ExecutionProjectRuleEditor from './ExecutionProjectRuleEditor';
import './execution.css';

const { Text } = Typography;

const releasePathFor = (workflow) => {
  const releaseId = workflow.active_release_id
    || workflow.current_release_id
    || workflow.release_id;
  if (releaseId) {
    return `/execution/admin/releases/${releaseId}`;
  }
  return `/execution/admin/releases?workflow_id=${encodeURIComponent(workflow.id || workflow.workflow_id)}`;
};

export default function ExecutionWorkflowAdmin() {
  const navigate = useNavigate();
  const [workflows, setWorkflows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [ruleEditorKey, setRuleEditorKey] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setWorkflows(await getExecutionWorkflows({ include_drafts: true, include_archived: true }));
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const columns = [
    {
      title: '流程',
      dataIndex: 'name',
      render: (value, row) => (
        <div className="execution-admin-workflow-name">
          <Text strong>{value || row.title}</Text>
          <span>
            <Tag color={row.management_mode === 'release_v2' ? 'purple' : 'default'}>
              {row.management_mode === 'release_v2' ? 'Release v2' : 'v1 草稿'}
            </Tag>
          </span>
          <Text type="secondary">{row.description || '暂无说明'}</Text>
          {row.match_rule && (
            <Text type="secondary">
              匹配规则：{row.match_rule.display_name || row.match_rule.rule_key}
              {row.match_rule.revision ? `（rev ${row.match_rule.revision}）` : ''}
              {row.match_rule.enabled === false && (
                <Tag color="warning" style={{ marginLeft: 4 }}>已停用</Tag>
              )}
            </Text>
          )}
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
      render: (value, row) => row.archived_at
        ? <Tag>已归档</Tag>
        : row.replacement_pending
          ? <Tag color="warning">待接替 / 已回切</Tag>
          : row.management_mode === 'release_v2'
        ? <Tag color="purple">Release 管理</Tag>
        : row.published_version
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
      width: 200,
      render: (_, row) => row.management_mode === 'release_v2' ? (
        <Button
          type="link"
          icon={<FileTextOutlined />}
          onClick={() => navigate(releasePathFor(row))}
        >
          Release 管理
        </Button>
      ) : (
        <Space size={0}>
          <Button
            type="link"
            icon={<EditOutlined />}
            onClick={() => navigate(`/execution/admin/workflows/${row.id || row.workflow_id}`)}
          >
            {row.archived_at ? '查看归档' : '设计'}
          </Button>
          {!row.archived_at && row.match_rule?.rule_key && (
            <Button
              type="link"
              onClick={() => setRuleEditorKey(row.match_rule.rule_key)}
            >
              匹配规则
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div className="execution-page execution-admin-page">
      <ExecutionChrome
        title="流程管理"
        subtitle="使用原生节点设计新流程；已发布流程保留版本和运行历史"
        backTo={{ path: '/execution', label: '流程目录' }}
        actions={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
            <Button
              icon={<FileTextOutlined />}
              onClick={() => navigate('/execution/admin/releases')}
            >
              Workflow Release v2
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/execution/admin/designer')}>
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
      <ExecutionProjectRuleEditor
        open={Boolean(ruleEditorKey)}
        ruleKey={ruleEditorKey}
        onClose={(saved) => {
          setRuleEditorKey(null);
          if (saved) {
            load();
          }
        }}
      />
    </div>
  );
}
