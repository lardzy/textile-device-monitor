import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Alert, Button, Result, Spin } from 'antd';
import { executionAuthApi } from '../../api/execution';

const ExecutionAuthContext = createContext(null);

export function ExecutionAuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await executionAuthApi.me();
      setUser(payload?.user || payload);
      setError(null);
    } catch (requestError) {
      if (requestError.status === 401) {
        setUser(null);
        setError(null);
      } else {
        setError(requestError);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const handleUnauthorized = () => {
      setUser(null);
      setLoading(false);
    };
    window.addEventListener('execution:unauthorized', handleUnauthorized);
    return () => window.removeEventListener('execution:unauthorized', handleUnauthorized);
  }, []);

  const login = useCallback(async (credentials) => {
    const payload = await executionAuthApi.login(credentials);
    const nextUser = payload?.user || payload;
    if (nextUser?.id || nextUser?.username) {
      setUser(nextUser);
    } else {
      await refresh();
    }
    return nextUser;
  }, [refresh]);

  const logout = useCallback(async () => {
    await executionAuthApi.logout();
    setUser(null);
  }, []);

  const value = useMemo(() => {
    const roleKeys = new Set([
      user?.role,
      ...(user?.roles || []).map(role => role.key || role.name || role),
    ].filter(Boolean));
    const permissionKeys = new Set([
      ...(user?.permissions || []).map(permission => permission.key || permission),
      ...(user?.roles || []).flatMap(role =>
        (role && typeof role === 'object' ? role.permissions || [] : [])
          .map(permission => permission.key || permission),
      ),
    ].filter(Boolean));
    const isAdmin = roleKeys.has('admin');
    const permissionsWereProvided = Array.isArray(user?.permissions)
      || (user?.roles || []).some(role => Array.isArray(role?.permissions));
    const defaultUserPermissions = new Set([
      'workflow.read',
      'workflow.run',
      'human_task.handle',
      'file.read',
      'file.write',
      'file.publish',
      'credential.manage',
    ]);
    const hasPermission = permissionKey =>
      isAdmin
      || permissionKeys.has(permissionKey)
      || (!permissionsWereProvided && roleKeys.has('user') && defaultUserPermissions.has(permissionKey));

    return {
      user,
      loading,
      error,
      refresh,
      login,
      logout,
      hasPermission,
      isAdmin,
      canRunWorkflow: hasPermission('workflow.run'),
      canDesignWorkflow: hasPermission('workflow.design'),
      canPublishWorkflow: hasPermission('workflow.publish'),
      canHandleHumanTasks: hasPermission('human_task.handle'),
      canManageUsers: hasPermission('user.manage'),
      canManageCredentials: hasPermission('credential.manage'),
    };
  }, [error, loading, login, logout, refresh, user]);

  return (
    <ExecutionAuthContext.Provider value={value}>
      {children}
    </ExecutionAuthContext.Provider>
  );
}

export const useExecutionAuth = () => {
  const value = useContext(ExecutionAuthContext);
  if (!value) {
    throw new Error('useExecutionAuth 必须在 ExecutionAuthProvider 中使用');
  }
  return value;
};

export function ExecutionProtectedRoute() {
  const { loading, user, error, refresh } = useExecutionAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="execution-route-loading">
        <Spin size="large" />
        <span>正在验证执行系统身份…</span>
      </div>
    );
  }

  if (error) {
    return (
      <Result
        status="warning"
        title="暂时无法连接执行系统"
        subTitle={error.message}
        extra={<Button type="primary" onClick={refresh}>重新连接</Button>}
      />
    );
  }

  if (!user) {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/execution/login?next=${encodeURIComponent(next)}`} replace />;
  }

  return <Outlet />;
}

export function ExecutionAuthShell() {
  return (
    <ExecutionAuthProvider>
      <Outlet />
    </ExecutionAuthProvider>
  );
}

export function ExecutionAdminRoute() {
  const { canDesignWorkflow } = useExecutionAuth();
  if (!canDesignWorkflow) {
    return (
      <div className="execution-access-denied">
        <Alert
          showIcon
          type="warning"
          message="需要流程设计权限"
          description="当前账号可以执行已发布流程，但不能编辑或发布流程。"
        />
      </div>
    );
  }
  return <Outlet />;
}
