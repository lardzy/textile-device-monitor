import { useEffect, useRef, useState } from 'react';
import { Avatar, Badge, Button, Dropdown, notification, Space, Tag, Typography } from 'antd';
import {
  LogoutOutlined,
  ApartmentOutlined,
  HistoryOutlined,
  InboxOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';
import { getHumanTasks } from '../../api/execution';
import { useExecutionAuth } from './ExecutionAuthContext';

const { Text } = Typography;

const ACTIVE_TASK_STATUSES = new Set(['open', 'pending', 'claimed']);
const TODO_POLL_INTERVAL_MS = 15000;

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
    canDesignWorkflow,
    logout,
  } = useExecutionAuth();

  const onInboxPage = location.pathname.startsWith('/execution/tasks');
  const [todoCount, setTodoCount] = useState(0);
  const [notificationApi, notificationHolder] = notification.useNotification();
  const seenTaskIdsRef = useRef(null);

  // 全局待办轮询：驱动头部角标；发现新任务时右下角气泡提醒。
  // 收件箱页面有自己的高频轮询，这里跳过避免重复请求。
  useEffect(() => {
    if (!canHandleHumanTasks || onInboxPage) {
      return undefined;
    }
    let cancelled = false;
    let timer = null;
    const poll = async () => {
      if (!document.hidden) {
        try {
          const items = await getHumanTasks();
          if (!cancelled) {
            const active = items.filter(task => ACTIVE_TASK_STATUSES.has(task.status));
            setTodoCount(active.length);
            const activeIds = new Set(active.map(task => String(task.id)));
            const seen = seenTaskIdsRef.current;
            if (seen === null) {
              // 首次载入只建立基线，不把存量任务当作“新任务”打扰用户。
              seenTaskIdsRef.current = activeIds;
            } else {
              const fresh = active.filter(task => !seen.has(String(task.id)));
              seenTaskIdsRef.current = activeIds;
              if (fresh.length > 0) {
                const first = fresh[0];
                notificationApi.info({
                  message: '有新的待办人工任务',
                  description: fresh.length > 1
                    ? `${first.title || '人工处理'} 等 ${fresh.length} 项任务等待处理`
                    : (first.title || '人工处理'),
                  placement: 'bottomRight',
                  btn: (
                    <Button
                      type="primary"
                      size="small"
                      onClick={() => {
                        notificationApi.destroy();
                        navigate('/execution/tasks');
                      }}
                    >
                      前往处理
                    </Button>
                  ),
                });
              }
            }
          }
        } catch (_error) {
          // 提醒轮询失败不影响当前页面，下个周期自动重试。
        }
      }
      if (!cancelled) {
        timer = window.setTimeout(poll, TODO_POLL_INTERVAL_MS);
      }
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, [canHandleHumanTasks, onInboxPage, navigate, notificationApi]);

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
      canDesignWorkflow && {
        key: 'workflow-admin',
        icon: <ApartmentOutlined />,
        label: '流程设计与发布',
        onClick: () => navigate('/execution/admin'),
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
      {notificationHolder}
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
        {location.pathname !== '/execution/runs' && (
          <Button
            type="text"
            icon={<HistoryOutlined />}
            onClick={() => navigate('/execution/runs')}
          >
            执行记录
          </Button>
        )}
        {canHandleHumanTasks && !onInboxPage && (
          <Badge count={todoCount} size="small" offset={[-2, 2]}>
            <Button
              type="text"
              icon={<InboxOutlined />}
              onClick={() => navigate('/execution/tasks')}
            >
              待办任务
            </Button>
          </Badge>
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
