import { Button, Form } from 'antd';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import NativeHumanTaskRenderer from './NativeHumanTaskRenderer';
import { extractExecutionResultFiles, extractPrimaryFileId } from './ExecutionResultFiles';

const item = id => ({
  id, kind: 'artifact', label: `${id}.xlsx`, root_id: 'records', relative_path: `${id}.xlsx`, fingerprint: '1:2',
  metadata: {
    presentation: 'result_file', name: `${id}.xlsx`, read_status: 'succeeded',
    result: { method: 'count', has_parts: true, parts: [{ name: '正面', components: [{ name: '棉', content: 70 }, { name: '粘纤', content: 30 }] }], remarks: [{ text: '同批样本' }], images: [] },
  },
});

function Selection({ onFinish, items = [item('a'), item('b')], allowedCounts, maximum = 2 }) {
  const [form] = Form.useForm();
  return <Form form={form} onFinish={onFinish}>
    <NativeHumanTaskRenderer renderer={{ capability: 'human.select' }} form={form}
      schema={{ properties: { selected_ids: { minItems: 1, maxItems: maximum }, primary_id: { type: 'string' } } }}
      inputData={{ items, allowed_selected_counts: allowedCounts }} />
    <Button htmlType="submit">提交选择</Button>
  </Form>;
}

describe('native result-file selection', () => {
  it('validates template counts and preserves user image order without embedded URLs', async () => {
    const submit = vi.fn();
    const user = userEvent.setup();
    const images = ['a', 'b', 'c'].map(id => ({ id, kind: 'image', label: `${id}.png`, relative_path: `folder/${id}.png`, metadata: {} }));
    render(<Selection onFinish={submit} items={images} allowedCounts={[1, 3]} maximum={3} />);
    await user.click(screen.getByRole('button', { name: '选择 a.png' }));
    await user.click(screen.getByRole('button', { name: '选择 b.png' }));
    await user.click(screen.getByRole('button', { name: '提交选择' }));
    expect(await screen.findByText('请选择 1、3 项')).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '选择 c.png' }));
    await user.click(screen.getAllByRole('button', { name: /前\s*移/ })[2]);
    await user.click(screen.getByRole('button', { name: '提交选择' }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ selected_ids: ['a', 'c', 'b'], primary_id: 'a' }));
  });
  it('uses result cards and sends only IDs and the selected primary', async () => {
    const submit = vi.fn();
    const user = userEvent.setup();
    render(<Selection onFinish={submit} />);
    expect(screen.getAllByText('棉')).toHaveLength(2);
    expect(screen.getAllByText('70%')).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: '提交选择' }));
    expect(submit).not.toHaveBeenCalled();
    await user.click(screen.getAllByRole('checkbox', { name: '需要' })[0]);
    await user.click(screen.getByRole('button', { name: '提交选择' }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ selected_ids: ['a'], primary_id: 'a' }));
  });

  it('projects native selection output into the existing result view', () => {
    const selection = { selected_items: [item('a')], primary_id: 'a' };
    const files = extractExecutionResultFiles(selection);
    expect(files[0].result.parts[0].components[0].content).toBe(70);
    expect(files[0].name).toBe('a.xlsx');
    expect(extractPrimaryFileId(selection)).toBe('a');
  });

  it('reuses the image gallery, previews images and submits only stable IDs', async () => {
    const submit = vi.fn();
    const user = userEvent.setup();
    const images = ['a', 'b'].map(id => ({
      id, kind: 'image', label: `${id}.png`, root_id: 'pictures', relative_path: `${id}.png`, fingerprint: '1:2',
      metadata: { name: `${id}.png`, preview_url: `/api/execution/v1/files/index/${id}/preview` },
    }));
    render(<Selection onFinish={submit} items={images} />);
    await user.click(screen.getByRole('button', { name: '查看大图 a.png' }));
    expect(screen.getByAltText('a.png')).toHaveAttribute('src', images[0].metadata.preview_url);
    await user.click(screen.getByRole('button', { name: /选为结果图片/ }));
    await user.click(screen.getByRole('button', { name: 'Close' }));
    await user.click(screen.getByRole('button', { name: '提交选择' }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ selected_ids: ['a'], primary_id: 'a' }));
  });

  it('displays paper-fiber qualitative results in the existing cards', async () => {
    const submit = vi.fn();
    const user = userEvent.setup();
    const paper = item('paper');
    paper.metadata.result = { worksheet: 'Sheet1', cell: 'W32', w32_value: '木浆 100', m32_value: '标准值', unit: '%' };
    render(<Selection onFinish={submit} items={[paper]} />);
    expect(screen.getByText('木浆 100')).toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: '需要' }));
    await user.click(screen.getByRole('button', { name: '提交选择' }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ selected_ids: ['paper'], primary_id: 'paper' }));
  });
});

function Groups({ onFinish, shared = false, initialValues }) {
  const [form] = Form.useForm();
  return <Form form={form} onFinish={onFinish} initialValues={initialValues}>
    <NativeHumanTaskRenderer renderer={{ capability: 'human.group_select' }} rendererContract={{ payload: {} }} form={form}
      inputData={{ ...(shared ? { context: { '任务单样品名称': '重装徒步冲锋衣 ７号（客户备注）' }, form_schema: {
          type: 'object', title: '各组共用的样品名称', required: ['sample_name'], properties: {
            sample_name: { type: 'string', title: '写入原始记录的样品名称', default: '', pattern: '\\S', 'x-suggestions': ['重装徒步冲锋衣', '冲锋衣', '客户备注'], 'x-suggestion-display': 'buttons' },
          },
        } } : {}), items: ['N_q01', 'W_q02', 'H_q01', 'H_q07'].map(id => ({ id, kind: 'image', label: id+'.bmp', relative_path: 'sample/'+id+'.bmp' })),
        groups: [{ id: 'n', label: '正面', allowed_selected_counts: [1] }, { id: 'w', label: '反面', allowed_selected_counts: [1] }, { id: 'h', label: '横截面', allowed_selected_counts: [1,2] }] }} />
    <Button htmlType="submit">提交分组</Button>
  </Form>;
}

it('submits each group with explicit ordering', async () => {
  const user = userEvent.setup(), submit = vi.fn();
  render(<Groups onFinish={submit} />);
  await user.type(screen.getAllByLabelText('筛选图片文件名')[0], 'N_');
  await user.click(screen.getAllByRole('button', { name: '选择 N_q01.bmp' })[0]);
  await user.click(screen.getAllByRole('button', { name: '选择 W_q02.bmp' })[0]);
  await user.click(screen.getAllByRole('button', { name: '选择 H_q07.bmp' })[1]);
  await user.click(screen.getAllByRole('button', { name: '选择 H_q01.bmp' })[1]);
  await user.click(screen.getAllByRole('button', { name: /前\s*移/ }).at(-1));
  await user.click(screen.getByRole('button', { name: '提交分组' }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith({ groups: [
    { id: 'n', selected_ids: ['N_q01'] }, { id: 'w', selected_ids: ['W_q02'] }, { id: 'h', selected_ids: ['H_q01','H_q07'] },
  ] }));
});

it('requires a shared name, offers editable candidates and preserves the answer when refreshed', async () => {
  const user = userEvent.setup(), submit = vi.fn();
  const draft = { groups: [{ id: 'n', selected_ids: ['N_q01'] }, { id: 'w', selected_ids: ['W_q02'] }, { id: 'h', selected_ids: ['H_q07'] }] };
  const view = render(<Groups shared onFinish={submit} initialValues={draft} />);
  expect(screen.getByText('重装徒步冲锋衣 ７号（客户备注）')).toBeInTheDocument();
  const name = screen.getByRole('textbox', { name: '写入原始记录的样品名称' });
  expect(name).toHaveValue('');
  await user.click(screen.getByRole('button', { name: '提交分组' }));
  expect(await screen.findByText('请填写写入原始记录的样品名称')).toBeInTheDocument();
  expect(submit).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '冲锋衣', exact: true }));
  expect(name).toHaveValue('冲锋衣');
  await user.clear(name);
  await user.type(name, '人工修订名称');
  view.rerender(<Groups shared onFinish={submit} initialValues={draft} />);
  expect(name).toHaveValue('人工修订名称');
  await user.click(screen.getByRole('button', { name: '提交分组' }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith({ ...draft, form_data: { sample_name: '人工修订名称' } }));
});

it('restores a saved shared name with the grouped image draft', () => {
  render(<Groups shared initialValues={{ form_data: { sample_name: '已保存的名称' } }} />);
  expect(screen.getByRole('textbox', { name: '写入原始记录的样品名称' })).toHaveValue('已保存的名称');
});
