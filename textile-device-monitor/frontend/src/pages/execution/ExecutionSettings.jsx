import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  EditOutlined,
  KeyOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  createExecutionUser,
  getExecutionCredentials,
  getExecutionUsers,
  updateExecutionUser,
  upsertExecutionCredential,
} from '../../api/execution';
import ExecutionChrome from './ExecutionChrome';
import { useExecutionAuth } from './ExecutionAuthContext';
import './execution.css';

const { Paragraph, Text, Title } = Typography;

const SYSTEMS = [
  {
    key: 'legacy_inspection',
    name: '旧检务系统',
    description: '用于 Windows Bridge 调用 FibreCheck；任务批准后可由已启用的 Bridge 领取。',
  },
  {
    key: 'new_inspection',
    name: '新检务系统',
    description: '用于后续网页连接器；获得内网和测试账号后再启用。',
  },
];

export default function ExecutionSettings() {
  const {
    canManageCredentials,
    canManageUsers,
    user,
  } = useExecutionAuth();
  const [credentialForm] = Form.useForm();
  const [userForm] = Form.useForm();
  const [credentials, setCredentials] = useState([]);
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [credentialSystem, setCredentialSystem] = useState(null);
  const [credentialSaving, setCredentialSaving] = useState(false);
  const [userModal, setUserModal] = useState(null);
  const [userSaving, setUserSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      if (canManageCredentials) {
        const credentialRows = await getExecutionCredentials();
        setCredentials(credentialRows);
      }
      if (canManageUsers) {
        setUsers(await getExecutionUsers());
      }
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [canManageCredentials, canManageUsers]);

  useEffect(() => {
    load();
  }, [load]);

  const credentialsBySystem = useMemo(() =>
    new Map(credentials.map(item => [item.system_key, item])), [credentials]);

  const openCredential = (system) => {
    const current = credentialsBySystem.get(system.key);
    credentialForm.setFieldsValue({
      account_name: current?.account_name,
      secret: '',
    });
    setCredentialSystem(system);
  };

  const saveCredential = async () => {
    try {
      const values = await credentialForm.validateFields();
      setCredentialSaving(true);
      await upsertExecutionCredential(credentialSystem.key, values);
      message.success(`${credentialSystem.name}凭据已加密保存`);
      setCredentialSystem(null);
      await load();
    } catch (requestError) {
      if (!requestError?.errorFields) {
        message.error(requestError.message || '凭据保存失败');
      }
    } finally {
      setCredentialSaving(false);
    }
  };

  const openCreateUser = () => {
    userForm.resetFields();
    userForm.setFieldsValue({ role: 'user', is_active: true });
    setUserModal({ mode: 'create' });
  };

  const openEditUser = (row) => {
    userForm.setFieldsValue({
      display_name: row.display_name,
      role: row.role,
      is_active: row.is_active,
      password: '',
    });
    setUserModal({ mode: 'edit', user: row });
  };

  const saveUser = async () => {
    try {
      const values = await userForm.validateFields();
      setUserSaving(true);
      if (userModal.mode === 'create') {
        await createExecutionUser(values);
      } else {
        const payload = { ...values };
        delete payload.username;
        if (!payload.password) {
          delete payload.password;
        }
        await updateExecutionUser(userModal.user.id, payload);
      }
      message.success(userModal.mode === 'create' ? '执行系统账号已创建' : '账号信息已更新');
      setUserModal(null);
      await load();
    } catch (requestError) {
      if (!requestError?.errorFields) {
        message.error(requestError.message || '账号保存失败');
      }
    } finally {
      setUserSaving(false);
    }
  };

  const credentialContent = (
    <div className="execution-settings__credentials">
      <Alert
        showIcon
        type="info"
        message="每位用户独立维护自己的外部系统账号"
        description="页面和接口不会回显密码；Windows Bridge 还会用本机受控口令核对任务绑定账号，管理员可通过 Bridge 令牌统一启停真实写入。"
      />
      <div className="execution-credential-grid">
        {SYSTEMS.map((system) => {
          const current = credentialsBySystem.get(system.key);
          return (
            <Card key={system.key} className="execution-credential-card">
              <div className="execution-credential-card__icon"><KeyOutlined /></div>
              <Title level={4}>{system.name}</Title>
              <Paragraph type="secondary">{system.description}</Paragraph>
              <div className="execution-credential-card__status">
                {current ? (
                  <Space direction="vertical" size={2}>
                    <Tag icon={<CheckCircleOutlined />} color="success">已配置</Tag>
                    <Text>{current.account_name || '未填写账号名'}</Text>
                    <Text type="secondary">
                      更新于 {dayjs(current.updated_at).format('YYYY-MM-DD HH:mm')}
                    </Text>
                  </Space>
                ) : (
                  <Tag>尚未配置</Tag>
                )}
                <Button icon={<EditOutlined />} onClick={() => openCredential(system)}>
                  {current ? '更新凭据' : '配置凭据'}
                </Button>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );

  const userColumns = [
    {
      title: '用户',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.display_name}</Text>
          <Text type="secondary">{row.username}</Text>
        </Space>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role',
      width: 120,
      render: value => (
        <Tag color={value === 'admin' ? 'blue' : 'default'}>
          {value === 'admin' ? '管理员' : '普通用户'}
        </Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 100,
      render: value => <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: value => dayjs(value).format('YYYY-MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 100,
      render: (_, row) => (
        <Button type="link" onClick={() => openEditUser(row)}>
          编辑
        </Button>
      ),
    },
  ];

  const tabs = [];
  if (canManageCredentials) {
    tabs.push({
      key: 'credentials',
      label: '我的外部系统凭据',
      children: credentialContent,
    });
  }
  if (canManageUsers) {
    tabs.push({
      key: 'users',
      label: '用户管理',
      children: (
        <div className="execution-settings__users">
          <div className="execution-settings__toolbar">
            <div>
              <Title level={4}>执行系统用户</Title>
              <Paragraph type="secondary">账号仅用于执行系统，不改变设备监控的内网免登录方式。</Paragraph>
            </div>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreateUser}>新增用户</Button>
          </div>
          <Table
            rowKey="id"
            columns={userColumns}
            dataSource={users}
            loading={loading}
            pagination={false}
          />
        </div>
      ),
    });
  }

  return (
    <div className="execution-page execution-settings">
      <ExecutionChrome
        title="账号与凭据"
        subtitle={`当前用户：${user?.display_name || user?.username}`}
        backTo={{ path: '/execution', label: '流程目录' }}
      />
      {error && (
        <Alert
          showIcon
          type="error"
          message="设置加载失败"
          description={error.message}
          action={<Button onClick={load}>重试</Button>}
        />
      )}
      <div className="execution-settings__body">
        {tabs.length ? (
          <Tabs items={tabs} />
        ) : (
          <Alert
            showIcon
            type="warning"
            message="当前账号没有可配置的设置项"
          />
        )}
      </div>

      <Modal
        title={`配置${credentialSystem?.name || ''}`}
        open={Boolean(credentialSystem)}
        okText="加密保存"
        cancelText="取消"
        confirmLoading={credentialSaving}
        onOk={saveCredential}
        onCancel={() => setCredentialSystem(null)}
        destroyOnClose
      >
        <Alert
          showIcon
          type="warning"
          message="保存后无法查看原密码，只能重新设置"
          style={{ marginBottom: 18 }}
        />
        <Form form={credentialForm} layout="vertical">
          <Form.Item name="account_name" label="账号">
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="secret"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={userModal?.mode === 'create' ? '新增执行系统用户' : '编辑用户'}
        open={Boolean(userModal)}
        okText="保存"
        cancelText="取消"
        confirmLoading={userSaving}
        onOk={saveUser}
        onCancel={() => setUserModal(null)}
        destroyOnClose
      >
        <Form form={userForm} layout="vertical">
          {userModal?.mode === 'create' && (
            <Form.Item
              name="username"
              label="登录账号"
              rules={[
                { required: true, message: '请输入登录账号' },
                { pattern: /^[A-Za-z0-9_.-]+$/, message: '仅支持字母、数字、点、横线和下划线' },
              ]}
            >
              <Input autoComplete="off" />
            </Form.Item>
          )}
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true, message: '请输入显示名称' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'user', label: '普通用户' },
                { value: 'admin', label: '管理员' },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="password"
            label={userModal?.mode === 'create' ? '初始密码' : '重置密码（留空则不修改）'}
            rules={[
              {
                required: userModal?.mode === 'create',
                message: '请输入初始密码',
              },
              { min: 10, message: '密码至少 10 位' },
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          {userModal?.mode === 'edit' && (
            <Form.Item name="is_active" label="启用状态" valuePropName="checked">
              <Switch checkedChildren="启用" unCheckedChildren="停用" />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  );
}
