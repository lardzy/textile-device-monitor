import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { server } from '../../../tests/testServer';
import ExecutionExternalOperationPanel from './ExecutionExternalOperationPanel';

const sourceSha256 = 'a'.repeat(64);
const operation = {
  id: 'operation-1',
  run_id: 'run-1',
  status: 'reconciliation_required',
  payload_checksum: 'b'.repeat(64),
  preflight_expires_at: '2026-08-01T08:00:00Z',
  approval: {
    approved_at: '2026-08-01T07:00:00Z',
    expires_at: '2026-08-01T07:15:00Z',
  },
  request_summary: {
    target_sample_number: '260187115-1',
    source_inspection_number: '260187115',
    inspector: '真实检验员',
    business_fields: {
      fiber_category: '棉再生纤',
      inspection_method: '定量',
      inspection_item: '棉再生纤定量-根数法',
      inspection_copies: 1,
    },
    files: [{
      id: 'file-1',
      filename: '260187115-根数法.xlsx',
      relative_path: '260187115-根数法.xlsx',
      content_sha256: sourceSha256,
      size_bytes: 1024,
      is_primary: true,
    }],
  },
  error: {
    code: 'transport_result_unknown',
    message: '写入边界后的响应丢失',
  },
};

const reconciliationContext = {
  operation,
  attempt: {
    id: 'attempt-1',
    attempt_no: 1,
    bridge_id: 'bridge-01',
    status: 'failed',
    current_stage: 'file_copy_started',
    error: operation.error,
  },
  expected_evidence: {
    target_sample_number: '260187115-1',
    payload_checksum: operation.payload_checksum,
    source_file_sha256: sourceSha256,
  },
};

const installHandlers = (onReconcile) => {
  server.use(
    http.get(
      '/api/execution/v1/auth/csrf',
      () => HttpResponse.json({ csrf_token: 'test-csrf-token' }),
    ),
    http.get(
      '/api/execution/v1/runs/run-1/external-operations',
      () => HttpResponse.json({ items: [operation] }),
    ),
    http.get(
      '/api/execution/v1/external-operations/operation-1/reconciliation',
      () => HttpResponse.json(reconciliationContext),
    ),
    http.post(
      '/api/execution/v1/external-operations/operation-1/reconcile',
      async ({ request }) => {
        const payload = await request.json();
        onReconcile?.(payload);
        return HttpResponse.json({
          duplicate: false,
          operation: {
            ...operation,
            status: payload.action === 'confirm_completed'
              ? 'completed'
              : 'failed',
          },
          remote_write_performed: payload.action === 'confirm_completed',
        });
      },
    ),
  );
};

describe('ExecutionExternalOperationPanel reconciliation', () => {
  it('普通运行用户只能看到锁定告警，不能录入对账结论', async () => {
    installHandlers();

    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canApprove
        canReconcile={false}
      />,
    );

    expect(
      await screen.findByText('存在执行结果未知的旧系统操作，禁止重试'),
    ).toBeInTheDocument();
    expect(screen.getByText('请联系管理员处置')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: '录入人工对账结果' }),
    ).not.toBeInTheDocument();
  });

  it('管理员提交完整写入的强证据载荷', async () => {
    const user = userEvent.setup();
    let submitted = null;
    const onChanged = vi.fn();
    installHandlers((payload) => {
      submitted = payload;
    });
    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canReconcile
        onChanged={onChanged}
      />,
    );

    await user.click(
      await screen.findByRole('button', { name: '录入人工对账结果' }),
    );
    expect(screen.getByText('预期检验员')).toBeInTheDocument();
    expect(screen.getAllByText('真实检验员')).not.toHaveLength(0);
    expect(screen.getByText('预期固定业务字段')).toBeInTheDocument();
    await user.click(
      await screen.findByRole('radio', {
        name: '已确认记录和文件均完整写入',
      }),
    );
    fireEvent.change(
      screen.getByLabelText('只读核验时间'),
      { target: { value: '2026-08-01T07:30:00' } },
    );
    await user.type(
      await screen.findByRole('spinbutton', { name: '目标精确记录数' }),
      '1',
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '目标文件数' }),
      '1',
    );
    await user.type(
      screen.getByRole('textbox', { name: '远端记录标识' }),
      'record-260187115-1',
    );
    await user.click(
      screen.getByRole('checkbox', {
        name: '只读回查的固定业务字段与预检单完全一致',
      }),
    );
    await user.click(
      screen.getByRole('checkbox', {
        name: '只读回查的检验员与预检单完全一致',
      }),
    );
    await user.type(
      screen.getByRole('textbox', { name: '远端文件 SHA-256' }),
      sourceSha256,
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账确认样品编号' }),
      '260187115-1',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账依据' }),
      '只读探针确认记录字段和远端文件哈希全部一致',
    );
    await user.click(
      screen.getByRole('button', { name: '确认完整写入并继续流程' }),
    );

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      action: 'confirm_completed',
      attempt_id: 'attempt-1',
      payload_checksum: operation.payload_checksum,
      confirmed_sample_number: '260187115-1',
      note: '只读探针确认记录字段和远端文件哈希全部一致',
      evidence: {
        exact_record_count: 1,
        target_file_count: 1,
        remote_record_id: 'record-260187115-1',
        business_fields_match: true,
        inspector_match: true,
        remote_file_sha256: sourceSha256,
      },
    });
    expect(
      Number.isNaN(Date.parse(submitted.evidence.checked_at)),
    ).toBe(false);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('管理员只有在三项计数均为零时才能确认完全未写入', async () => {
    const user = userEvent.setup();
    let submitted = null;
    installHandlers((payload) => {
      submitted = payload;
    });
    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canReconcile
      />,
    );

    await user.click(
      await screen.findByRole('button', { name: '录入人工对账结果' }),
    );
    await user.click(
      await screen.findByRole('radio', {
        name: '已确认记录和文件均完全不存在',
      }),
    );
    fireEvent.change(
      screen.getByLabelText('只读核验时间'),
      { target: { value: '2026-08-01T07:31:00' } },
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '目标精确记录数' }),
      '0',
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '包含匹配记录数' }),
      '0',
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '目标文件数' }),
      '0',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账确认样品编号' }),
      '260187115-1',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账依据' }),
      '只读探针确认精确、包含和文件计数均为零',
    );
    await user.click(
      screen.getByRole('button', {
        name: '确认完全未写入并失败收尾',
      }),
    );

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      action: 'confirm_no_side_effect',
      attempt_id: 'attempt-1',
      evidence: {
        exact_record_count: 0,
        contains_record_count: 0,
        target_file_count: 0,
      },
    });
  });

  it('未填写只读探针核验时间时不会提交', async () => {
    const user = userEvent.setup();
    let submitted = null;
    installHandlers((payload) => {
      submitted = payload;
    });
    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canReconcile
      />,
    );

    await user.click(
      await screen.findByRole('button', { name: '录入人工对账结果' }),
    );
    await user.click(
      await screen.findByRole('radio', {
        name: '已确认记录和文件均完全不存在',
      }),
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '目标精确记录数' }),
      '0',
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '包含匹配记录数' }),
      '0',
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '目标文件数' }),
      '0',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账确认样品编号' }),
      '260187115-1',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账依据' }),
      '只读探针报告尚未填写核验时间',
    );
    await user.click(
      screen.getByRole('button', {
        name: '确认完全未写入并失败收尾',
      }),
    );

    expect(submitted).toBeNull();
    expect(
      await screen.findByText('请填写只读探针实际完成核验的时间'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('dialog', { name: '录入旧系统人工对账结论' }),
    ).toBeInTheDocument();
  });

  it('证据过期的 409 保留表单且重试请求体保持不变', async () => {
    const user = userEvent.setup();
    const submitted = [];
    installHandlers();
    server.use(
      http.post(
        '/api/execution/v1/external-operations/operation-1/reconcile',
        async ({ request }) => {
          submitted.push(await request.json());
          return HttpResponse.json({
            code: 'external_reconciliation_evidence_stale',
            message: '只读对账证据已过期，请重新核验',
          }, { status: 409 });
        },
      ),
    );
    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canReconcile
      />,
    );

    await user.click(
      await screen.findByRole('button', { name: '录入人工对账结果' }),
    );
    await user.click(
      await screen.findByRole('radio', {
        name: '已确认记录和文件均完全不存在',
      }),
    );
    fireEvent.change(
      screen.getByLabelText('只读核验时间'),
      { target: { value: '2026-08-01T07:32:00' } },
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '目标精确记录数' }),
      '0',
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '包含匹配记录数' }),
      '0',
    );
    await user.type(
      screen.getByRole('spinbutton', { name: '目标文件数' }),
      '0',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账确认样品编号' }),
      '260187115-1',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账依据' }),
      '使用只读探针核验三项计数',
    );
    const submitButton = screen.getByRole('button', {
      name: '确认完全未写入并失败收尾',
    });
    await user.click(submitButton);

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(
      await screen.findByText('只读对账证据已过期，请重新核验'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('textbox', { name: '对账依据' }),
    ).toHaveValue('使用只读探针核验三项计数');
    expect(screen.getByLabelText('只读核验时间')).toHaveValue(
      '2026-08-01T07:32',
    );

    await user.click(submitButton);
    await waitFor(() => expect(submitted).toHaveLength(2));
    expect(submitted[1]).toEqual(submitted[0]);
  });
});
