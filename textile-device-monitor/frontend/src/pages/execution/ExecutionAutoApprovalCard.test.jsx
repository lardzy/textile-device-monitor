import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { server } from '../../../tests/testServer';
import ExecutionAutoApprovalCard from './ExecutionAutoApprovalCard';

const preparedOperation = {
  id: 'op-1',
  status: 'prepared',
  payload_checksum: 'checksum-1',
  request_summary: {
    operation_type: 'legacy_special_wool_qualitative_upload',
    target_sample_number: '26W006736-1',
    target_filename: '26W006736-1-107-8A-record.xls',
    business_fields: {
      fiber_category: '棉再生纤',
      inspection_item: '棉再生纤定性',
    },
    safety: { execution_available: true },
    files: [],
  },
};

const csrfHandler = http.get('/api/execution/v1/auth/csrf', () =>
  HttpResponse.json({ csrf_token: 'test-csrf' }));

const renderCard = () => render(
  <ExecutionAutoApprovalCard
    runId="run-1"
    refreshKey="t0"
    canApprove
    onChanged={() => Promise.resolve()}
  />,
);

describe('ExecutionAutoApprovalCard', () => {
  it('倒计时 5 秒后自动批准预检单', async () => {
    let approveCalls = 0;
    let approveBody = null;
    server.use(
      csrfHandler,
      http.get('/api/execution/v1/runs/run-1/external-operations', () =>
        HttpResponse.json({ items: [preparedOperation] })),
      http.post('/api/execution/v1/external-operations/op-1/approve', async ({ request }) => {
        approveCalls += 1;
        approveBody = await request.json();
        return HttpResponse.json({ id: 'op-1', status: 'approved' });
      }),
    );

    renderCard();
    expect(await screen.findByText('26W006736-1')).toBeInTheDocument();
    expect(screen.getByText(/秒后自动批准/)).toBeInTheDocument();

    await waitFor(
      () => expect(approveCalls).toBe(1),
      { timeout: 8000 },
    );
    expect(approveBody).toMatchObject({
      approved: true,
      payload_checksum: 'checksum-1',
      confirmed_sample_number: '26W006736-1',
    });
  }, 10000);

  it('终止自动批准后保持待批准，可手动批准', async () => {
    let approveCalls = 0;
    server.use(
      csrfHandler,
      http.get('/api/execution/v1/runs/run-1/external-operations', () =>
        HttpResponse.json({ items: [preparedOperation] })),
      http.post('/api/execution/v1/external-operations/op-1/approve', () => {
        approveCalls += 1;
        return HttpResponse.json({ id: 'op-1', status: 'approved' });
      }),
    );

    const user = userEvent.setup();
    renderCard();
    expect(await screen.findByText('26W006736-1')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '终止自动批准' }));
    expect(screen.getByText('自动批准已终止')).toBeInTheDocument();
    // 超过完整倒计时周期后仍未自动批准
    await new Promise(resolve => { setTimeout(resolve, 5500); });
    expect(approveCalls).toBe(0);

    await user.click(screen.getByRole('button', { name: '核对无误，批准此预检单' }));
    await waitFor(() => expect(approveCalls).toBe(1));
  }, 12000);

  it('纸类判定登记在自动批准前展示人工确认的标准值', async () => {
    const paperJudgementOperation = {
      ...preparedOperation,
      request_summary: {
        ...preparedOperation.request_summary,
        operation_type: 'legacy_generic_check_record_entry',
        target_sample_number: '260174495',
        result_contract: {
          worksheet: 'Sheet1',
          cell: 'W32',
          value: '木浆 100',
          unit: '%',
        },
        judgement_contract: {
          required: true,
          judge_basis: '按客户要求',
          judgement: '符合',
          standard_value: '定性，100%木浆',
        },
      },
    };
    server.use(
      csrfHandler,
      http.get('/api/execution/v1/runs/run-1/external-operations', () =>
        HttpResponse.json({ items: [paperJudgementOperation] })),
    );

    renderCard();
    expect(await screen.findByText('260174495')).toBeInTheDocument();
    expect(screen.getByText('Sheet1!W32')).toBeInTheDocument();
    expect(screen.getByText('木浆 100%')).toBeInTheDocument();
    expect(screen.getByText('判定依据')).toBeInTheDocument();
    expect(screen.getByText('按客户要求')).toBeInTheDocument();
    expect(screen.getByText('判定结果')).toBeInTheDocument();
    expect(screen.getByText('符合')).toBeInTheDocument();
    expect(screen.getByText('标准值与允差（人工确认）')).toBeInTheDocument();
    expect(screen.getByText('定性，100%木浆')).toBeInTheDocument();
  });

  it('没有待处理操作时渲染为空', async () => {
    server.use(
      csrfHandler,
      http.get('/api/execution/v1/runs/run-1/external-operations', () =>
        HttpResponse.json({ items: [] })),
    );
    const { container } = renderCard();
    await waitFor(() => expect(container.firstChild).toBeNull());
  });
});
