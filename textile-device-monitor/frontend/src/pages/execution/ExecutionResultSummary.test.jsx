import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import ExecutionResultSummary from './ExecutionResultSummary';

const microscopyOutputs = {
  original_record: {
    filename: '260191285-39-8B-纤维形状截面定量试验-2026.xls',
    size_bytes: 1296384,
    download_url: '/api/execution/v1/artifacts/art-1/download',
  },
  upload: { status: 'completed', target_sample_number: '260191285-1' },
  review: { status: 'completed', target_sample_number: '260191285-1' },
  judgement: {
    judgement_required: false,
    auto_submitted: true,
    auto_submit_reason: 'judgement_not_required',
  },
  registration_decision: {
    registration_cancelled: false,
    existing_record_action: 'append',
    expected_existing_register_count: 1,
    selected_project: {
      check_item_name: '纤维横截面',
      sample_identify: '贴肤层面料',
    },
  },
  registration_workbook: {
    filename: '260191285-纤维横截面-检验记录登记.xls',
    size_bytes: 29184,
    download_url: '/api/execution/v1/artifacts/art-2/download',
  },
  final_entry: {
    status: 'completed',
    target_sample_number: '260191285',
    receipt: {
      final_entry: {
        proofed: true,
        expected_existing_register_count: 1,
        resulting_register_count: 2,
      },
    },
  },
};

describe('ExecutionResultSummary', () => {
  it('按业务顺序渲染结构化摘要而不是原始 JSON', () => {
    render(<ExecutionResultSummary outputs={microscopyOutputs} />);

    expect(screen.getByText('生成原始记录')).toBeInTheDocument();
    expect(screen.getByText(
      '260191285-39-8B-纤维形状截面定量试验-2026.xls',
    )).toBeInTheDocument();
    expect(screen.getByText('1.2 MB')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /下载/ })).toHaveLength(2);

    expect(screen.getByText('旧系统上传')).toBeInTheDocument();
    expect(screen.getByText('旧系统复核')).toBeInTheDocument();
    expect(screen.getAllByText('260191285-1')).toHaveLength(2);

    expect(screen.getByText('判定信息')).toBeInTheDocument();
    expect(screen.getByText('任务单未要求判定，未写入判定字段'))
      .toBeInTheDocument();

    expect(screen.getByText('登记决策')).toBeInTheDocument();
    expect(screen.getByText('纤维横截面')).toBeInTheDocument();
    expect(screen.getByText('贴肤层面料')).toBeInTheDocument();
    expect(screen.getByText('确认后直接新增')).toBeInTheDocument();

    expect(screen.getByText('检验记录登记')).toBeInTheDocument();
    expect(screen.getByText('1 → 2 条')).toBeInTheDocument();
    expect(screen.getByText('已校对')).toBeInTheDocument();
    expect(screen.getAllByText('已完成').length).toBeGreaterThanOrEqual(3);

    // 结构化渲染后不直接暴露大段 JSON
    expect(screen.queryByText(/payload_checksum/)).not.toBeInTheDocument();
  });

  it('取消录入时展示明确的取消状态', () => {
    render(
      <ExecutionResultSummary
        outputs={{
          registration_decision: {
            registration_cancelled: true,
            existing_record_action: 'cancel',
          },
        }}
      />,
    );
    expect(screen.getByText('已取消录入')).toBeInTheDocument();
    expect(screen.getByText('用户在登记前选择取消，本次未写入检验记录'))
      .toBeInTheDocument();
  });

  it('判定结果与样品识别在要求判定时逐行展示', () => {
    render(
      <ExecutionResultSummary
        outputs={{
          judgement: {
            judgement_required: true,
            sample_identity: '正面',
            judge_basis: 'GB/T 4688-2020',
            standard_value: '木浆 100',
            judgement: '符合',
          },
        }}
      />,
    );
    expect(screen.getByText('判定依据')).toBeInTheDocument();
    expect(screen.getByText('GB/T 4688-2020')).toBeInTheDocument();
    expect(screen.getByText('标准值与允差')).toBeInTheDocument();
    expect(screen.getByText('木浆 100')).toBeInTheDocument();
    expect(screen.getByText('符合')).toBeInTheDocument();
  });

  it('跳过已由结果文件卡片展示的键，未知键归入其它输出', () => {
    render(
      <ExecutionResultSummary
        outputs={{
          selected_files: [{ id: 'f-1', result: { w32_value: '木浆 100' } }],
          qualitative_result: { w32_value: '木浆 100' },
          custom_metric: { value: 42 },
        }}
        skipKeys={new Set(['selected_files', 'qualitative_result'])}
      />,
    );
    expect(screen.queryByText('selected_files')).not.toBeInTheDocument();
    expect(screen.getByText('其它输出')).toBeInTheDocument();
    expect(screen.getByText('custom_metric')).toBeInTheDocument();
    expect(screen.getByText(/"value": 42/)).toBeInTheDocument();
  });

  it('原始 JSON 默认折叠，展开后可查看', async () => {
    const user = userEvent.setup();
    render(<ExecutionResultSummary outputs={microscopyOutputs} />);

    expect(screen.queryByText(/"proofed": true/)).not.toBeInTheDocument();
    await user.click(screen.getByText('原始输出数据（JSON）'));
    expect(screen.getByText(/"proofed": true/)).toBeInTheDocument();
  });

  it('输出为空时不渲染任何内容', () => {
    const { container } = render(<ExecutionResultSummary outputs={{}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
