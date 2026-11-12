import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ExecutionRequestError from './ExecutionRequestError';

describe('ExecutionRequestError', () => {
  it('显示多个具体问题，并按索引或节点 ID 定位名称', () => {
    render(<ExecutionRequestError
      error={{ message: '流程定义校验失败', requestId: 'request-123', details: { issues: [
        { code: 'unknown_node_type', message: '未知节点类型或版本：file.old@1', path: '$.nodes[1]' },
        { code: 'reference_invalid', message: '来源节点不存在', path: '$.nodes[finish].input_mapping.result' },
      ] } }}
      definition={{ nodes: [{ id: 'start' }, { id: 'query', name: '查询原始资料' }, { id: 'finish', name: '返回结果' }] }}
    />);
    const items = within(screen.getByRole('list', { name: '具体错误' })).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('查询原始资料： 未知节点类型或版本：file.old@1');
    expect(items[1]).toHaveTextContent('返回结果： 来源节点不存在');
    expect(items[1]).toHaveTextContent('位置：$.nodes[finish].input_mapping.result');
    expect(screen.getByText('请求编号：request-123')).toBeInTheDocument();
  });

  it('普通错误可读，缺失消息时使用默认提示，不展开请求及其他字段', () => {
    const { rerender } = render(<ExecutionRequestError error={{ message: '网络连接失败', body: { password: 'private-password' } }} />);
    expect(screen.getByRole('alert')).toHaveTextContent('网络连接失败');
    expect(screen.queryByText('private-password')).not.toBeInTheDocument();
    rerender(<ExecutionRequestError fallback="无法开始执行" error={{ details: { issues: [null, { input: 'private-input' }, { message: {} }] } }} />);
    expect(screen.getByRole('alert')).toHaveTextContent('无法开始执行');
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    expect(screen.queryByText('private-input')).not.toBeInTheDocument();
  });
});
