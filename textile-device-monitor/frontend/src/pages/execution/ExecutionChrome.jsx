import { Avatar, Button, Dropdown, Space, Tag, Typography } from 'antd';
import {
  LogoutOutlined,
  InboxOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';
import { useExecutionAuth } from './ExecutionAuthContext';

const { Text } = Typography;

export default function ExecutionChrome({
  title,
  subtitle,
  backTo,
  onBack,
  actions,
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const {
    user,
    isAdmin,
    canManageCredentials,
    canManageUsers,
    canHandleHumanTasks,
    logout,
  } = useExecutionAuth();

  const handleLogout = async () => {
    await logout();
    navigate('/execution/login', { replace: true });
  };

  const accountMenu = {
    items: [
      {
        key: 'identity',
        disabled: true,
        label: (
          <div className="execution-account-summary">
            <Text strong>{user?.display_name || user?.name || user?.username}</Text>
            <Text type="secondary">{isAdmin ? '管理员' : '执行用户'}</Text>
          </div>
        ),
      },
      { type: 'divider' },
      canHandleHumanTasks && {
        key: 'tasks',
        icon: <InboxOutlined />,
        label: '人工任务收件箱',
        onClick: () => navigate('/execution/tasks'),
      },
      (canManageCredentials || canManageUsers) && {
        key: 'settings',
        icon: <SettingOutlined />,
        label: '账号与外部系统凭据',
        onClick: () => navigate('/execution/settings'),
      },
      {
        key: 'logout',
        icon: <LogoutOutlined />,
        label: '退出执行系统',
        onClick: handleLogout,
      },
    ].filter(Boolean),
  };

  return (
    <header className="execution-page-header">
      <div className="execution-page-header__identity">
        {backTo && (
          <Button
            type="text"
            onClick={() => (
              onBack ? onBack(backTo) : navigate(backTo.path)
            )}
          >
            ← {backTo.label}
          </Button>
        )}
        <div>
          <div className="execution-page-header__title-row">
            <h1>{title}</h1>
            {isAdmin && (
              <Tag icon={<SafetyCertificateOutlined />} color="blue">管理员</Tag>
            )}
          </div>
          {subtitle && <p>{subtitle}</p>}
        </div>
      </div>
      <Space size={12}>
        {canHandleHumanTasks && !location.pathname.startsWith('/execution/tasks') && (
          <Button
            type="text"
            icon={<InboxOutlined />}
            onClick={() => navigate('/execution/tasks')}
          >
            待办任务
          </Button>
        )}
        {actions}
        <Dropdown menu={accountMenu} trigger={['click']}>
          <Button className="execution-account-button">
            <Avatar size={24} icon={<UserOutlined />} />
            <span>{user?.display_name || user?.name || user?.username}</span>
          </Button>
        </Dropdown>
      </Space>
    </header>
  );
}
