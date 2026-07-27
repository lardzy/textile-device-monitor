import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import useExecutionEvents, { parseExecutionEventBlock } from './useExecutionEvents';

const encoder = new TextEncoder();

const responseFrom = (text, { keepOpen = false } = {}) => {
  let streamController;
  const body = new ReadableStream({
    start(controller) {
      streamController = controller;
      if (text) {
        controller.enqueue(encoder.encode(text));
      }
      if (!keepOpen) {
        controller.close();
      }
    },
  });
  return {
    response: {
      ok: true,
      status: 200,
      body,
    },
    close: () => streamController?.close(),
  };
};

describe('useExecutionEvents', () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    vi.unstubAllGlobals();
    globalThis.fetch = originalFetch;
  });

  it('解析后端 stream.rotate 事件', () => {
    expect(parseExecutionEventBlock(
      'event: stream.rotate\ndata: {"reason":"connection_lifetime_reached"}',
    )).toEqual({
      id: null,
      type: 'stream.rotate',
      data: { reason: 'connection_lifetime_reached' },
    });
  });

  it('收到 stream.rotate 后立即重连，并在重连成功后要求重拉快照', async () => {
    const rotated = responseFrom(
      'event: stream.rotate\ndata: {"reason":"connection_lifetime_reached"}\n\n',
    );
    const connected = responseFrom('', { keepOpen: true });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(rotated.response)
      .mockResolvedValueOnce(connected.response);
    vi.stubGlobal('fetch', fetchMock);
    const onEvent = vi.fn();
    const onReconnect = vi.fn();

    const { unmount } = renderHook(() => useExecutionEvents('run-1', {
      onEvent,
      onReconnect,
    }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'stream.rotate' }));
    expect(onReconnect).toHaveBeenCalledTimes(1);
    unmount();
    connected.close();
  });

  it('普通断线重连时携带 Last-Event-ID，并重拉完整快照', async () => {
    const disconnected = responseFrom(
      'id: 7\nevent: node.completed\ndata: {"sequence":7}\n\n',
    );
    const connected = responseFrom('', { keepOpen: true });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(disconnected.response)
      .mockResolvedValueOnce(connected.response);
    vi.stubGlobal('fetch', fetchMock);
    const onReconnect = vi.fn();

    const { unmount } = renderHook(() => useExecutionEvents('run-1', {
      onReconnect,
    }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2), { timeout: 2500 });
    expect(fetchMock.mock.calls[1][1].headers['Last-Event-ID']).toBe('7');
    expect(onReconnect).toHaveBeenCalledTimes(1);
    unmount();
    connected.close();
  });
});
