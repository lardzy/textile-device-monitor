import { render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { server } from '../../../tests/testServer';
import {
  ExecutionAuthProvider,
  useExecutionAuth,
} from './ExecutionAuthContext';

function ReconciliationPermissionProbe() {
  const { canReconcileExternalOperations } = useExecutionAuth();
  return (
    <span data-testid="can-reconcile">
      {String(canReconcileExternalOperations)}
    </span>
  );
}

describe('ExecutionAuthContext reconciliation permission', () => {
  it.each([
    {
      name: '非管理员即使被授予专用权限也不能人工对账',
      user: {
        id: 'user-with-permission',
        username: 'operator',
        role: 'user',
        permissions: ['external_operation.reconcile'],
      },
      expected: 'false',
    },
    {
      name: '管理员缺少专用权限时不能人工对账',
      user: {
        id: 'admin-without-permission',
        username: 'admin-without-permission',
        role: 'admin',
        permissions: ['workflow.run'],
      },
      expected: 'false',
    },
    {
      name: '管理员同时持有专用权限时可以人工对账',
      user: {
        id: 'reconciliation-admin',
        username: 'reconciliation-admin',
        role: 'admin',
        permissions: ['workflow.run', 'external_operation.reconcile'],
      },
      expected: 'true',
    },
  ])('$name', async ({ user, expected }) => {
    server.use(
      http.get(
        '/api/execution/v1/auth/me',
        () => HttpResponse.json({ user }),
      ),
    );

    render(
      <ExecutionAuthProvider>
        <ReconciliationPermissionProbe />
      </ExecutionAuthProvider>,
    );

    expect(await screen.findByTestId('can-reconcile')).toHaveTextContent(
      expected,
    );
  });
});
