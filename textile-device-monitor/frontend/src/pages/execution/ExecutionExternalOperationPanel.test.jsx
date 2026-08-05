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
const finalEntrySummaryChecksum = 'c'.repeat(64);
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

const finalEntryReconciliationOperation = {
  ...operation,
  request_summary: {
    ...operation.request_summary,
    operation_type: 'legacy_microscopy_check_record_entry',
    target_sample_number: '260111037',
    source_inspection_number: '260111037',
    final_entry_summary: {
      source_review_target_sample_number: '260111037-1',
      expected_task_check_count: 1,
      expected_existing_register_count: 1,
      resulting_register_count: 2,
    },
  },
};

const finalEntryReconciliationContext = {
  evidence_kind: 'microscopy_final_entry_v1',
  operation: finalEntryReconciliationOperation,
  attempt: {
    ...reconciliationContext.attempt,
    id: 'attempt-final-entry',
    attempt_no: 3,
    current_stage: 'excel_collection_started',
  },
  expected_evidence: {
    target_sample_number: '260111037',
    payload_checksum: finalEntryReconciliationOperation.payload_checksum,
    source_file_sha256: sourceSha256,
    evidence_contract: 'microscopy_final_entry_v1',
    final_entry_summary_checksum: finalEntrySummaryChecksum,
    expected_existing_register_count: 1,
    writer_stage: 'excel_collection_started',
    confirm_completed: {
      expected_existing_register_count: 1,
      resulting_register_count: 2,
      actual_register_count: 2,
      actual_file_reference_count: 2,
      actual_key_result_count: 2,
      actual_proofed_count: 2,
      target_file_count: 1,
      writer_stage: 'excel_collection_started',
      allowed_writer_stages: [
        'excel_collection_started',
        'remote_file_copy_started',
      ],
    },
    confirm_no_side_effect: {
      expected_existing_register_count: 1,
      actual_register_count: 1,
      actual_file_reference_count: 1,
      actual_key_result_count: 1,
      actual_proofed_count: 1,
      target_file_count: 0,
      writer_stage: 'excel_collection_started',
      allowed_writer_stages: [
        'authenticated',
        'excel_collection_started',
      ],
    },
  },
};

const installFinalEntryReconciliationHandlers = (onReconcile) => {
  server.use(
    http.get(
      '/api/execution/v1/runs/run-1/external-operations',
      () => HttpResponse.json({
        items: [finalEntryReconciliationOperation],
      }),
    ),
    http.get(
      '/api/execution/v1/external-operations/operation-1/reconciliation',
      () => HttpResponse.json(finalEntryReconciliationContext),
    ),
    http.post(
      '/api/execution/v1/external-operations/operation-1/reconcile',
      async ({ request }) => {
        const payload = await request.json();
        onReconcile?.(payload);
        return HttpResponse.json({
          duplicate: false,
          operation: {
            ...finalEntryReconciliationOperation,
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
  it('检验记录登记受控例外在卡片和确认弹窗明确展示', async () => {
    const user = userEvent.setup();
    const controlledReason = '已获准验证 CheckCount=1 时追加一条登记记录';
    const finalEntryOperation = {
      ...operation,
      status: 'prepared',
      error: null,
      request_summary: {
        ...operation.request_summary,
        operation_type: 'legacy_microscopy_check_record_entry',
        target_sample_number: '260111037',
        source_inspection_number: '260111037',
        final_entry_summary: {
          source_review_target_sample_number: '260111037-1',
          image_count: 7,
          expected_task_check_count: 1,
          expected_existing_register_count: 1,
          resulting_register_count: 2,
          controlled_test: true,
          controlled_test_reason: controlledReason,
        },
        safety: { execution_available: true },
        // 即使服务端误将私有字段透出，页面也不应展示或依赖它。
        machine_payload: {
          secret_marker: 'PRIVATE-MACHINE-PAYLOAD-MUST-NOT-RENDER',
        },
      },
    };
    installHandlers();
    server.use(
      http.get(
        '/api/execution/v1/runs/run-1/external-operations',
        () => HttpResponse.json({ items: [finalEntryOperation] }),
      ),
    );

    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canApprove
      />,
    );

    expect(
      await screen.findByText('检验记录登记与校对'),
    ).toBeInTheDocument();
    expect(screen.getByText('来源特纤号')).toBeInTheDocument();
    expect(screen.getByText('260111037-1')).toBeInTheDocument();
    expect(screen.getByText('1 条 → 2 条')).toBeInTheDocument();
    expect(screen.getByText('受控例外原因')).toBeInTheDocument();
    expect(screen.getAllByText(controlledReason)).not.toHaveLength(0);
    expect(
      screen.queryByText('PRIVATE-MACHINE-PAYLOAD-MUST-NOT-RENDER'),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole('button', { name: '核对并批准预检单' }),
    );

    expect(
      screen.getByRole('dialog', {
        name: '最终核对本次检验记录登记与校对预检单',
      }),
    ).toBeInTheDocument();
    expect(screen.getAllByText('260111037-1')).toHaveLength(2);
    expect(screen.getAllByText('1 条 → 2 条')).toHaveLength(2);
    expect(
      screen.getByText('请确认旧记录保持不变，本次只新增一条。', {
        exact: false,
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText('PRIVATE-MACHINE-PAYLOAD-MUST-NOT-RENDER'),
    ).not.toBeInTheDocument();
  });

  it('特种毛图片上传在实机语义未验证时只展示预检', async () => {
    installHandlers();
    server.use(
      http.get(
        '/api/execution/v1/runs/run-1/external-operations',
        () => HttpResponse.json({
          items: [{
            ...operation,
            status: 'prepared',
            error: null,
            request_summary: {
              ...operation.request_summary,
              operation_type: 'legacy_special_wool_image_upload',
              safety: { execution_available: false },
              execution_capability: {
                available: false,
                message: '图片子记录语义尚未完成实机证明',
              },
              business_fields: {
                fiber_category: '图片',
                inspection_method: '',
                inspection_item: '图片',
                inspection_copies: 1,
              },
            },
          }],
        }),
      ),
    );

    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canApprove
      />,
    );

    expect(
      await screen.findByText('旧系统上传-特种毛-图片'),
    ).toBeInTheDocument();
    expect(screen.getAllByText('仅预检')).not.toHaveLength(0);
    expect(
      screen.queryByRole('button', { name: '核对并批准预检单' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText('图片子记录语义尚未完成实机证明'),
    ).toBeInTheDocument();
  });

  it('特种毛图片上传明确展示含最终编号的模板文件名', async () => {
    const user = userEvent.setup();
    const targetFilename = (
      '260111037-2-39-8B-纤维形状截面定量试验-2026.xls'
    );
    installHandlers();
    server.use(
      http.get(
        '/api/execution/v1/runs/run-1/external-operations',
        () => HttpResponse.json({
          items: [{
            ...operation,
            status: 'prepared',
            error: null,
            request_summary: {
              ...operation.request_summary,
              operation_type: 'legacy_special_wool_image_upload',
              target_sample_number: '260111037-2',
              target_filename: targetFilename,
              safety: { execution_available: true },
              execution_capability: { available: true },
              business_fields: {
                fiber_category: '图片',
                inspection_method: '',
                inspection_item: '图片',
                inspection_copies: 1,
              },
            },
          }],
        }),
      ),
    );

    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canApprove
      />,
    );

    expect(await screen.findByText(targetFilename)).toBeInTheDocument();
    await user.click(
      screen.getByRole('button', { name: '核对并批准预检单' }),
    );
    expect(
      screen.getByRole('dialog', {
        name: '最终核对本次上传预检单',
      }),
    ).toHaveTextContent(targetFilename);
  });

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

  it('检验记录登记按专用契约提交未新增写入证据', async () => {
    const user = userEvent.setup();
    let submitted = null;
    installHandlers();
    installFinalEntryReconciliationHandlers((payload) => {
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
    expect(screen.getByText('对账证据契约')).toBeInTheDocument();
    expect(screen.getByText('微观形貌检验记录登记 v1')).toBeInTheDocument();
    expect(screen.getByText('写入器失败阶段')).toBeInTheDocument();
    await user.click(
      screen.getByRole('radio', {
        name: '已确认原有记录未变且没有产生新增写入',
      }),
    );
    expect(
      screen.getByText('实际登记记录数（预期为 1）'),
    ).toBeInTheDocument();
    fireEvent.change(
      screen.getByLabelText('只读核验时间'),
      { target: { value: '2026-08-05T10:30:00' } },
    );
    for (const name of [
      '实际登记记录数',
      '实际文件引用数',
      '实际关键结果数',
      '实际已校对记录数',
    ]) {
      await user.type(screen.getByRole('spinbutton', { name }), '1');
    }
    await user.type(
      screen.getByRole('spinbutton', { name: '目标文件数' }),
      '0',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账确认样品编号' }),
      '260111037',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账依据' }),
      '只读探针确认原有登记、文件引用、关键结果和校对状态均未变化',
    );
    await user.click(
      screen.getByRole('button', {
        name: '确认未产生新增写入并失败收尾',
      }),
    );

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      action: 'confirm_no_side_effect',
      attempt_id: 'attempt-final-entry',
      confirmed_sample_number: '260111037',
      evidence: {
        evidence_contract: 'microscopy_final_entry_v1',
        final_entry_summary_checksum: finalEntrySummaryChecksum,
        expected_existing_register_count: 1,
        actual_register_count: 1,
        actual_file_reference_count: 1,
        actual_key_result_count: 1,
        actual_proofed_count: 1,
        target_file_count: 0,
        writer_stage: 'excel_collection_started',
      },
    });
    expect(submitted.evidence).not.toHaveProperty('exact_record_count');
    expect(submitted.evidence).not.toHaveProperty('contains_record_count');
  });

  it('检验记录登记按专用契约提交完整新增与校对证据', async () => {
    const user = userEvent.setup();
    let submitted = null;
    installHandlers();
    installFinalEntryReconciliationHandlers((payload) => {
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
      screen.getByRole('radio', {
        name: '已确认新增登记记录、文件和校对均完整',
      }),
    );
    fireEvent.change(
      screen.getByLabelText('只读核验时间'),
      { target: { value: '2026-08-05T10:31:00' } },
    );
    for (const name of [
      '实际登记记录数',
      '实际文件引用数',
      '实际关键结果数',
      '实际已校对记录数',
    ]) {
      await user.type(screen.getByRole('spinbutton', { name }), '2');
    }
    await user.type(
      screen.getByRole('spinbutton', { name: '目标文件数' }),
      '1',
    );
    await user.type(
      screen.getByRole('textbox', { name: '远端记录标识' }),
      'sha256:new-final-entry',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账确认样品编号' }),
      '260111037',
    );
    await user.type(
      screen.getByRole('textbox', { name: '对账依据' }),
      '只读探针确认新增登记、文件引用、关键结果和校对均完整',
    );
    await user.click(
      screen.getByRole('button', {
        name: '确认新增记录完整并继续流程',
      }),
    );

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      action: 'confirm_completed',
      attempt_id: 'attempt-final-entry',
      evidence: {
        evidence_contract: 'microscopy_final_entry_v1',
        final_entry_summary_checksum: finalEntrySummaryChecksum,
        expected_existing_register_count: 1,
        resulting_register_count: 2,
        actual_register_count: 2,
        actual_file_reference_count: 2,
        actual_key_result_count: 2,
        actual_proofed_count: 2,
        target_file_count: 1,
        writer_stage: 'excel_collection_started',
        remote_record_id: 'sha256:new-final-entry',
      },
    });
    expect(submitted.evidence).not.toHaveProperty('remote_file_sha256');
    expect(submitted.evidence).not.toHaveProperty('business_fields_match');
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

  it('纸类上传、复核和检验记录登记使用明确的预检与状态文案', async () => {
    const paperBaseSummary = {
      source_inspection_number: '26W006687',
      target_sample_number: '26W006687-1',
      result_contract: {
        worksheet: 'Sheet1',
        cell: 'W32',
        value: '木浆 100',
        unit: '%',
      },
      business_fields: {
        fiber_category: '棉再生纤',
        inspection_method: '定量',
        inspection_item: '棉再生纤定性',
        inspection_copies: 1,
      },
      safety: { execution_available: true },
    };
    const paperOperations = [{
      id: 'paper-upload',
      status: 'prepared',
      payload_checksum: '1'.repeat(64),
      preflight_expires_at: '2026-08-05T12:00:00Z',
      request_summary: {
        ...paperBaseSummary,
        operation_type: 'legacy_special_wool_qualitative_upload',
        inspector: '李舒洋',
        target_filename: '26W006687-1-26W006687-纸浆纤维鉴别.xls',
        files: [{
          id: 'paper-source',
          filename: '26W006687-纸浆纤维鉴别.xls',
          relative_path: '26W006687/26W006687-纸浆纤维鉴别.xls',
          size_bytes: 84992,
          content_sha256: 'a'.repeat(64),
          is_primary: true,
        }],
      },
    }, {
      id: 'paper-review',
      status: 'approved',
      payload_checksum: '2'.repeat(64),
      preflight_expires_at: '2026-08-05T12:00:00Z',
      approval: { approved_at: '2026-08-05T09:00:00Z' },
      request_summary: {
        ...paperBaseSummary,
        operation_type: 'legacy_special_wool_qualitative_review',
        business_fields: {
          fiber_category: '棉再生纤',
          review_action: '特纤复核',
          review_item: '棉再生纤定性',
          review_copies: 1,
        },
      },
    }, {
      id: 'paper-final-entry',
      status: 'completed',
      payload_checksum: '3'.repeat(64),
      preflight_expires_at: '2026-08-05T12:00:00Z',
      request_summary: {
        ...paperBaseSummary,
        operation_type: 'legacy_generic_check_record_entry',
        target_sample_number: '26W006687',
        safety: {
          execution_available: true,
          proof_required: false,
        },
      },
    }];
    server.use(
      http.get(
        '/api/execution/v1/runs/run-1/external-operations',
        () => HttpResponse.json({ items: paperOperations }),
      ),
    );
    const user = userEvent.setup();
    render(
      <ExecutionExternalOperationPanel
        runId="run-1"
        canApprove
      />,
    );

    expect(await screen.findByText('旧系统上传-纸类定性原始记录'))
      .toBeInTheDocument();
    expect(screen.getByText('旧系统-纸类特纤复核')).toBeInTheDocument();
    expect(screen.getByText('检验记录登记-纸类定性结果')).toBeInTheDocument();
    expect(screen.getByText(/请核对目标编号、前缀文件名/)).toBeInTheDocument();
    expect(screen.getByText(/正在等待或执行 Windows Bridge/)).toBeInTheDocument();
    expect(screen.getByText(/旧系统回执已核对/)).toBeInTheDocument();
    expect(screen.getByText('26W006687-1-26W006687-纸浆纤维鉴别.xls'))
      .toBeInTheDocument();
    expect(screen.getAllByText('Sheet1!W32')).not.toHaveLength(0);
    expect(screen.getAllByText('木浆 100')).not.toHaveLength(0);
    expect(screen.getByText('保存记录，不执行校对')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '核对并批准预检单' }));
    const dialog = screen.getByRole('dialog', {
      name: '最终核对本次纸类原始记录上传预检单',
    });
    expect(dialog).toHaveTextContent('26W006687-1');
    expect(dialog).toHaveTextContent('Sheet1!W32');
    expect(dialog).toHaveTextContent('木浆 100%');
  });
});
