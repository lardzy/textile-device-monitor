import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../../tests/testServer';
import ExecutionProjectRuleEditor from './ExecutionProjectRuleEditor';

const paperRule = {
  rule_key: 'paper_gbt4688_qualitative',
  display_name: '纸、纸板和纸浆纤维鉴别分析 GB/T 4688-2020（定性）',
  category_key: 'other',
  enabled: true,
  revision: 3,
  config: {
    default_node_type: 'file.paper_fiber_gbt4688_qualitative',
    source: {
      root_id: 'paper_fiber_records',
      folder_match: {
        strategy: 'level1_contains_number',
        entry_kind: 'workbook',
        max_depth: 2,
      },
    },
    task_facts: [
      {
        condition_key: 'task_item_name',
        fact: 'check_item_name',
        op: 'in',
        values: ['纸、纸板和纸浆纤维鉴别分析'],
      },
      {
        condition_key: 'test_method',
        fact: 'check_method',
        op: 'eq',
        value: 'GB/T 4688-2020',
      },
    ],
    probes: [
      {
        name: 'qualitative_result',
        type: 'cell_value',
        sheet: 'Sheet1',
        cell: 'W32',
        parser: 'paper_qualitative_v1',
      },
    ],
    binding: null,
  },
  updated_by_id: null,
  created_at: '2026-08-15T00:00:00Z',
  updated_at: '2026-08-15T01:00:00Z',
};

const renderEditor = (onClose = vi.fn()) => {
  render(
    <ExecutionProjectRuleEditor
      open
      ruleKey="paper_gbt4688_qualitative"
      onClose={onClose}
    />,
  );
  return onClose;
};

describe('ExecutionProjectRuleEditor', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/execution/v1/auth/csrf', () =>
        HttpResponse.json({ csrf_token: 'csrf-test' })),
      http.get('/api/execution/v1/project-rules/paper_gbt4688_qualitative', () =>
        HttpResponse.json({ item: paperRule }),
      ),
      http.get('/api/execution/v1/files/roots', () =>
        HttpResponse.json({
          items: [
            { root_id: 'paper_fiber_records', name: '纸类原始记录' },
          ],
        }),
      ),
    );
  });

  it('加载并展示规则字段', async () => {
    renderEditor();
    await waitFor(() => {
      expect(
        screen.getByDisplayValue('纸、纸板和纸浆纤维鉴别分析 GB/T 4688-2020（定性）'),
      ).toBeInTheDocument();
    });
    expect(screen.getByText('paper_gbt4688_qualitative')).toBeInTheDocument();
    expect(screen.getByText('rev 3')).toBeInTheDocument();
    expect(screen.getByDisplayValue('GB/T 4688-2020')).toBeInTheDocument();
    expect(screen.getByText('纸、纸板和纸浆纤维鉴别分析')).toBeInTheDocument();
    expect(screen.getByDisplayValue('W32')).toBeInTheDocument();
  });

  it('保存时按表单重建 config 并上送', async () => {
    let captured = null;
    server.use(
      http.put(
        '/api/execution/v1/project-rules/paper_gbt4688_qualitative',
        async ({ request }) => {
          captured = await request.json();
          return HttpResponse.json({
            item: { ...paperRule, revision: 4, ...captured },
          });
        },
      ),
    );
    const onClose = renderEditor();
    await waitFor(() => {
      expect(screen.getByDisplayValue('GB/T 4688-2020')).toBeInTheDocument();
    });
    const methodInput = screen.getByDisplayValue('GB/T 4688-2020');
    await userEvent.clear(methodInput);
    await userEvent.type(methodInput, 'GB/T 4688-2030');
    await userEvent.click(screen.getByRole('button', { name: /保存并即时生效/ }));
    await waitFor(() => {
      expect(captured).not.toBeNull();
    });
    expect(captured.display_name).toBe(
      '纸、纸板和纸浆纤维鉴别分析 GB/T 4688-2020（定性）',
    );
    expect(captured.config.default_node_type).toBe(
      'file.paper_fiber_gbt4688_qualitative',
    );
    const methodFact = captured.config.task_facts.find(
      fact => fact.condition_key === 'test_method',
    );
    expect(methodFact.values).toEqual(['GB/T 4688-2030']);
    expect(captured.config.probes[0].cell).toBe('W32');
    await waitFor(() => {
      expect(onClose).toHaveBeenCalledWith(true);
    });
  });

  it('测试匹配干跑并展示结果', async () => {
    let captured = null;
    server.use(
      http.post('/api/execution/v1/project-rules/test', async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json({
          rule_key: 'paper_gbt4688_qualitative',
          rule_revision: 3,
          matched_conditions: ['source_root', 'folder', 'task_item_name', 'test_method'],
          full_match: true,
          index_state: 'ready',
          query_state: 'validated',
          task_cache_state: 'ready',
          candidate_count: 1,
          candidate_preview: { name: '26W006701 记录.xls' },
        });
      }),
    );
    renderEditor();
    await waitFor(() => {
      expect(screen.getByDisplayValue('GB/T 4688-2020')).toBeInTheDocument();
    });
    await userEvent.type(
      screen.getByPlaceholderText('输入检验编号，如 26W006701'),
      '26W006701',
    );
    await userEvent.click(screen.getByRole('button', { name: /测试匹配/ }));
    await waitFor(() => {
      expect(screen.getByText('完全匹配')).toBeInTheDocument();
    });
    expect(captured.rule_key).toBe('paper_gbt4688_qualitative');
    expect(captured.inspection_number).toBe('26W006701');
    expect(captured.config.source.root_id).toBe('paper_fiber_records');
    expect(screen.getByText(/26W006701 记录\.xls/)).toBeInTheDocument();
  });
});
