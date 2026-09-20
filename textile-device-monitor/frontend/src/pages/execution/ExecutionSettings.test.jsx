import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ExecutionSettings from './ExecutionSettings';
import { getExecutionCredentialCatalog, upsertExecutionCredential } from '../../api/execution';

vi.mock('../../api/execution', () => ({ getExecutionCredentialCatalog: vi.fn(), upsertExecutionCredential: vi.fn(),
  createExecutionUser: vi.fn(), getExecutionUsers: vi.fn(), updateExecutionUser: vi.fn() }));
vi.mock('./ExecutionAuthContext', () => ({ useExecutionAuth: () => ({ canManageCredentials: true, canManageUsers: false, user: { display_name: '测试' } }) }));
vi.mock('./ExecutionChrome', () => ({ default: () => null }));

describe('adapter credentials', () => {
  it('configures an installed system without a frontend system list change', async () => {
    getExecutionCredentialCatalog.mockResolvedValue({ items: [], systems: [{ key: 'lab.example', name: '实验室示例', description: '适配器' }] });
    upsertExecutionCredential.mockResolvedValue({});
    render(<ExecutionSettings />);
    await screen.findByText('实验室示例');
    fireEvent.click(screen.getByRole('button', { name: /配置凭据/ }));
    fireEvent.change(screen.getByLabelText('账号'), { target: { value: 'tester' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'offline-only' } });
    fireEvent.click(screen.getByRole('button', { name: /保存/ }));
    await waitFor(() => expect(upsertExecutionCredential).toHaveBeenCalledWith('lab.example', { account_name: 'tester', secret: 'offline-only' }));
  });
});
