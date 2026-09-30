import { Form } from 'antd';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import SchemaFields from './SchemaFields';

const schema = {
  type: 'object',
  properties: {
    standard_value: {
      type: 'string',
      title: '标准值与允差',
      default: '木浆 100',
      'x-copy-sources': [
        { label: '任务单说明列', text: '定性，100%木浆' },
        { label: 'Sheet1!M32', text: '100%木浆' },
      ],
    },
  },
  required: ['standard_value'],
};

describe('SchemaFields copy sources', () => {
  it('按所选项目显示必填字段并预填唯一选项', async () => {
    const user = userEvent.setup();
    const conditional = {
      properties: {
        project: { type: 'string', title: '检测项目', enum: ['a', 'b'], default: 'a' },
        result: { type: 'string', 'x-hidden': true },
      },
      allOf: [{
        if: { properties: { project: { const: 'b' } }, required: ['project'] },
        then: { properties: { result: { type: 'string', title: '判定结果', enum: ['符合'], default: '符合' } }, required: ['result'] },
      }],
    };
    render(<Form><SchemaFields schema={conditional} /></Form>);
    expect(screen.queryByRole('combobox', { name: '判定结果' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: '检测项目' }));
    await user.click(await screen.findByText('b', { selector: '.ant-select-item-option-content' }));
    expect(await screen.findByRole('combobox', { name: '判定结果' })).toBeInTheDocument();
    expect(screen.getByText('符合', { selector: '.ant-select-selection-item' })).toBeInTheDocument();
  });

  it('对象和未指定元素类型的数组可编辑为 JSON，并阻止无效类型提交', async () => {
    const submit = vi.fn();
    const user = userEvent.setup();
    render(
      <Form onFinish={submit}>
        <SchemaFields schema={{
          properties: {
            source: { type: 'object', title: '工作簿引用', default: { root_id: 'source', relative_path: 'before.xlsx' } },
            writes: { type: 'array', title: '写入计划', minItems: 1 },
          },
          required: ['source', 'writes'],
        }} />
        <button type="submit">提交 JSON</button>
      </Form>,
    );
    const source = screen.getByRole('textbox', { name: '工作簿引用' });
    const writes = screen.getByRole('textbox', { name: '写入计划' });
    expect(JSON.parse(source.value)).toEqual({ root_id: 'source', relative_path: 'before.xlsx' });
    fireEvent.change(source, { target: { value: '{broken' } });
    fireEvent.change(writes, { target: { value: '{}' } });
    await user.click(screen.getByRole('button', { name: '提交 JSON' }));
    expect(await screen.findByText('工作簿引用必须是有效的 JSON 对象')).toBeInTheDocument();
    expect(await screen.findByText('写入计划必须是数组')).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
    const sourceValue = { root_id: 'source', relative_path: 'after.xlsx' };
    const writesValue = [{ sheet: 'Sheet1', cell: 'B2', value: 'after' }];
    fireEvent.change(source, { target: { value: JSON.stringify(sourceValue) } });
    fireEvent.change(writes, { target: { value: JSON.stringify(writesValue) } });
    await user.click(screen.getByRole('button', { name: '提交 JSON' }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ source: sourceValue, writes: writesValue }));
  });

  it('预填 W32，并可从任务单说明列或 M32 一键填入', async () => {
    const user = userEvent.setup();
    render(
      <Form>
        <SchemaFields schema={schema} />
      </Form>,
    );

    const input = screen.getByDisplayValue('木浆 100');
    expect(screen.getByText('任务单说明列：')).toBeInTheDocument();
    expect(screen.getByText('定性，100%木浆')).toBeInTheDocument();
    expect(screen.getByText('Sheet1!M32：')).toBeInTheDocument();
    expect(screen.getByText('100%木浆')).toBeInTheDocument();

    const fillButtons = screen.getAllByRole('button', { name: '填入' });
    await user.click(fillButtons[0]);
    expect(input).toHaveValue('定性，100%木浆');
    await user.click(fillButtons[1]);
    expect(input).toHaveValue('100%木浆');
  });

  it('样品识别支持可编辑候选并显示份数不一致警告', async () => {
    const user = userEvent.setup();
    render(
      <Form>
        <SchemaFields
          schema={{
            type: 'object',
            'x-warning': '任务单检测份数为 3，样品识别拆分后为 2 项。',
            properties: {
              sample_identity: {
                type: 'string',
                title: '样品识别',
                'x-suggestions': ['正面', '反面'],
              },
            },
            required: ['sample_identity'],
          }}
        />
      </Form>,
    );

    expect(screen.getByText('任务单份数与样品识别数量不一致')).toBeInTheDocument();
    const input = screen.getByRole('combobox', { name: '样品识别' });
    await user.click(input);
    expect((await screen.findAllByText('正面')).length).toBeGreaterThan(0);
    await user.clear(input);
    await user.type(input, '反面');
    expect(input).toHaveValue('反面');
  });

  it('分词按钮直接填入嵌套字段，保留手工修改并提交单一字符串', async () => {
    const user = userEvent.setup(), submit = vi.fn();
    render(<Form onFinish={submit}>
      <SchemaFields namePrefix="shared" schema={{ properties: {
        name: { type: 'string', title: '样品名称', default: '', 'x-suggestion-display': 'buttons',
          'x-suggestions': ['冲锋衣', '冲锋衣', '纱布'], 'x-suggestion-label': '分词建议' },
      }, required: ['name'] }} />
      <button type="submit">提交</button>
    </Form>);
    const input = screen.getByRole('textbox', { name: '样品名称' });
    expect(screen.getAllByRole('button', { name: '冲锋衣', exact: true })).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: '冲锋衣', exact: true }));
    expect(submit).not.toHaveBeenCalled();
    expect(input).toHaveValue('冲锋衣');
    expect(screen.getByRole('button', { name: '冲锋衣', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await user.type(input, '面料');
    expect(input).toHaveValue('冲锋衣面料');
    await user.click(screen.getByRole('button', { name: '提交', exact: true }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ shared: { name: '冲锋衣面料' } }));
  });

  it('没有分词候选时仍能输入，已冻结字段的按钮和输入均不可修改', async () => {
    const user = userEvent.setup();
    const view = render(<Form><SchemaFields schema={{ properties: {
      name: { type: 'string', title: '样品名称', 'x-suggestion-display': 'buttons', 'x-suggestions': [] },
    } }} /></Form>);
    expect(screen.getByText('暂无可用建议，请直接输入。')).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '样品名称' }), '手工样品');
    view.rerender(<Form><SchemaFields schema={{ properties: {
      name: { type: 'string', title: '样品名称', readOnly: true, 'x-suggestion-display': 'buttons', 'x-suggestions': ['其他'] },
    } }} /></Form>);
    expect(screen.getByRole('textbox', { name: '样品名称' })).toHaveValue('手工样品');
    expect(screen.getByRole('textbox', { name: '样品名称' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '其他', exact: true })).toBeDisabled();
  });
});
