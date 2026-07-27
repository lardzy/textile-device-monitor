import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clearExecutionRunRequest,
  prepareExecutionRunRequest,
  resetExecutionRunRequestCache,
  runRequestSignature,
} from './executionRunRequest';

describe('execution run idempotency', () => {
  beforeEach(() => {
    resetExecutionRunRequestCache();
  });

  it('相同业务载荷在失败重试时复用幂等键，且不受对象键顺序影响', () => {
    const first = prepareExecutionRunRequest({
      workflow_id: 'wf-1',
      inspection_number: ' 26X1 ',
      input_data: { remark: '复核', inspection_number: '26X1' },
      global_data: { priority: 1 },
    });
    const retry = prepareExecutionRunRequest({
      workflow_id: 'wf-1',
      inspection_number: '26X1',
      input_data: { inspection_number: '26X1', remark: '复核' },
      global_data: { priority: 1 },
    });

    expect(retry.signature).toBe(first.signature);
    expect(retry.payload.idempotency_key).toBe(first.payload.idempotency_key);
  });

  it('成功后清除缓存，同一载荷的新运行会获得新键', () => {
    const first = prepareExecutionRunRequest({
      workflow_id: 'wf-1',
      inspection_number: '26X1',
      input_data: {},
      global_data: {},
    });
    clearExecutionRunRequest(first.signature);
    const next = prepareExecutionRunRequest({
      workflow_id: 'wf-1',
      inspection_number: '26X1',
      input_data: {},
      global_data: {},
    });

    expect(next.payload.idempotency_key).not.toBe(first.payload.idempotency_key);
  });

  it('载荷变化会使用不同幂等键', () => {
    const base = {
      workflow_id: 'wf-1',
      inspection_number: '26X1',
      global_data: {},
    };
    const first = prepareExecutionRunRequest({
      ...base,
      input_data: { sample_type: '棉' },
    });
    const changed = prepareExecutionRunRequest({
      ...base,
      input_data: { sample_type: '麻' },
    });

    expect(runRequestSignature(first.payload)).not.toBe(runRequestSignature(changed.payload));
    expect(changed.payload.idempotency_key).not.toBe(first.payload.idempotency_key);
  });

  it('页面模块重新加载后仍从 sessionStorage 复用待确认请求键', async () => {
    const payload = {
      workflow_id: 'wf-1',
      inspection_number: '26X1',
      input_data: { inspection_number: '26X1', sample_type: '棉' },
      global_data: { priority: 'normal' },
    };
    const first = prepareExecutionRunRequest(payload);

    vi.resetModules();
    const reloadedModule = await import('./executionRunRequest.js');
    const afterRefresh = reloadedModule.prepareExecutionRunRequest(payload);

    expect(afterRefresh.payload.idempotency_key).toBe(first.payload.idempotency_key);
  });

  it('忽略损坏或过期的 sessionStorage 缓存', () => {
    sessionStorage.setItem('textile.execution.pending-run-requests.v1', '{not-json');
    const request = prepareExecutionRunRequest({
      workflow_id: 'wf-1',
      inspection_number: '26X1',
      input_data: {},
      global_data: {},
    });

    expect(request.payload.idempotency_key).toBeTruthy();
    expect(() => JSON.parse(
      sessionStorage.getItem('textile.execution.pending-run-requests.v1'),
    )).not.toThrow();
  });
});
