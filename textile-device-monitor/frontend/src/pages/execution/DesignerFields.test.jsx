import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ValueEditor } from './DesignerFields';

function Field({ initial }) {
  const [value, setValue] = useState(initial);
  return <><ValueEditor schema={{}} value={value} onChange={setValue} label="字段值" /><output data-testid="value">{JSON.stringify(value)}</output></>;
}

describe('untyped configuration values', () => {
  it('edits workbook paths without converting objects into text', () => {
    render(<Field initial={{ path: '#/inspection_number' }} />);
    fireEvent.change(screen.getByRole('textbox', { name: '字段值 path' }), { target: { value: '#/sample_identity' } });
    expect(screen.getByTestId('value').textContent).toBe('{"path":"#/sample_identity"}');
    expect(screen.queryByDisplayValue('[object Object]')).not.toBeInTheDocument();
  });
  it('keeps numeric-looking text and explicitly changes its value type', () => {
    render(<Field initial="260191178" />);
    fireEvent.change(screen.getByRole('textbox', { name: '字段值' }), { target: { value: '00100' } });
    expect(screen.getByTestId('value').textContent).toBe('"00100"');
    fireEvent.mouseDown(screen.getByRole('combobox', { name: '字段值 值类型' }));
    fireEvent.click(screen.getByText('对象', { selector: '.ant-select-item-option-content' }));
    expect(screen.getByTestId('value').textContent).toBe('{}');
    const name = screen.getByRole('searchbox', { name: '添加属性 字段值' });
    fireEvent.change(name, { target: { value: 'path' } });
    fireEvent.click(screen.getByRole('button', { name: /添\s*加/ }));
    fireEvent.change(screen.getByRole('textbox', { name: '字段值 path' }), { target: { value: '#/inspection_number' } });
    expect(screen.getByTestId('value').textContent).toBe('{"path":"#/inspection_number"}');
  });
});
