import { useEffect, useState } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, Button, Form, Input, Typography, message } from 'antd';
import {
  ApartmentOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useExecutionAuth } from './ExecutionAuthContext';
import './execution.css';

const { Paragraph, Text, Title } = Typography;

export default function ExecutionLogin() {
  const [form] = Form.useForm();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { login, user, loading, error } = useExecutionAuth();
  const [submitting, setSubmitting] = useState(false);
  const next = searchParams.get('next');
  const safeNext = next?.startsWith('/execution') ? next : '/execution';

  useEffect(() => {
    form.getFieldInstance('username')?.focus?.();
  }, [form]);

  if (!loading && user) {
    return <Navigate to={safeNext} replace />;
  }

  const handleFinish = async (values) => {
    setSubmitting(true);
    try {
      await login(values);
      message.success('登录成功');
      navigate(safeNext, { replace: true });
    } catch (loginError) {
      message.error(loginError.message || '登录失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="execution-login">
      <section className="execution-login__intro">
        <div className="execution-login__brand">
          <ApartmentOutlined />
          <span>检测执行系统</span>
        </div>
        <Title level={1}>让复杂检测流程<br />清晰、可靠地执行</Title>
        <Paragraph className="execution-login__description">
          统一组织原始记录、人工复核、结果制品和外部系统衔接，
          每一步都可恢复、可追踪、可审计。
        </Paragraph>
        <div className="execution-login__security">
          <SafetyCertificateOutlined />
          <div>
            <Text strong>独立权限边界</Text>
            <Text type="secondary">仅执行系统需要登录，设备监控保持原有使用方式。</Text>
          </div>
        </div>
      </section>

      <section className="execution-login__panel">
        <div className="execution-login__form">
          <Text className="execution-eyebrow">EXECUTION SYSTEM</Text>
          <Title level={2}>登录执行系统</Title>
          <Paragraph
            type="secondary"
            className="execution-login__form-subtitle"
          >
            请使用为您分配的内部账号
          </Paragraph>
          {error && (
            <Alert
              showIcon
              type="warning"
              message="认证服务暂不可用"
              description={error.message}
              style={{ marginBottom: 20 }}
            />
          )}
          <Form
            form={form}
            layout="vertical"
            size="large"
            onFinish={handleFinish}
            requiredMark={false}
          >
            <Form.Item
              name="username"
              label="账号"
              rules={[{ required: true, message: '请输入账号' }]}
            >
              <Input prefix={<UserOutlined />} autoComplete="username" />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                autoComplete="current-password"
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              登录
            </Button>
          </Form>
          <Text type="secondary" className="execution-login__hint">
            凭据仅通过安全会话使用，不会写入浏览器本地存储。
          </Text>
        </div>
      </section>
    </div>
  );
}
