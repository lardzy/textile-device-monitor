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

function Selection({ onFinish }) {
  const [form] = Form.useForm();
  return <Form form={form} onFinish={onFinish}>
    <NativeHumanTaskRenderer renderer={{ capability: 'human.select' }} form={form}
      schema={{ properties: { selected_ids: { minItems: 1, maxItems: 2 }, primary_id: { type: 'string' } } }}
      inputData={{ items: [item('a'), item('b')] }} />
    <Button htmlType="submit">提交选择</Button>
  </Form>;
}

describe('native result-file selection', () => {
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
});
