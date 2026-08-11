import { Form } from 'antd';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
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
});
