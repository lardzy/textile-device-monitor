import { describe, expect, it } from 'vitest';
import { createClientUuid } from './clientId';

const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('createClientUuid', () => {
  it('优先使用可用的 randomUUID', () => {
    const expected = '12345678-1234-4234-9234-123456789abc';
    expect(createClientUuid({ randomUUID: () => expected })).toBe(expected);
  });

  it('在局域网 HTTP 环境缺少 randomUUID 时使用 getRandomValues', () => {
    const uuid = createClientUuid({
      getRandomValues: (bytes) => {
        bytes.fill(0xff);
        return bytes;
      },
    });

    expect(uuid).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff');
    expect(uuid).toMatch(UUID_V4_PATTERN);
  });

  it('Web Crypto 完全不可用时仍返回格式合法的 UUID', () => {
    expect(createClientUuid(null)).toMatch(UUID_V4_PATTERN);
  });

  it('randomUUID 调用失败时自动回退', () => {
    const uuid = createClientUuid({
      randomUUID: () => {
        throw new Error('Only secure origins are allowed');
      },
      getRandomValues: bytes => bytes.fill(0),
    });

    expect(uuid).toBe('00000000-0000-4000-8000-000000000000');
  });
});
