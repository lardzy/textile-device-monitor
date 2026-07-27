import { useEffect, useRef } from 'react';
import { executionEventsUrl } from '../../api/execution';

export const parseExecutionEventBlock = (block) => {
  let id = null;
  let type = 'message';
  const data = [];
  block.split(/\r?\n/).forEach((line) => {
    if (line.startsWith('id:')) {
      id = line.slice(3).trim();
    } else if (line.startsWith('event:')) {
      type = line.slice(6).trim() || 'message';
    } else if (line.startsWith('data:')) {
      data.push(line.slice(5).trimStart());
    }
  });
  if (!data.length) {
    return null;
  }
  const raw = data.join('\n');
  try {
    return { id, type, data: JSON.parse(raw) };
  } catch {
    return { id, type, data: raw };
  }
};

export default function useExecutionEvents(runId, {
  enabled = true,
  onEvent,
  onReconnect,
  onConnectionChange,
} = {}) {
  const callbacksRef = useRef({ onEvent, onReconnect, onConnectionChange });

  useEffect(() => {
    callbacksRef.current = { onEvent, onReconnect, onConnectionChange };
  }, [onConnectionChange, onEvent, onReconnect]);

  useEffect(() => {
    if (!runId || !enabled) {
      return undefined;
    }

    const controller = new AbortController();
    let lastEventId = '';
    let retryCount = 0;
    let retryTimer = null;

    const connect = async () => {
      try {
        const headers = {
          Accept: 'text/event-stream',
        };
        if (lastEventId) {
          headers['Last-Event-ID'] = lastEventId;
        }
        const response = await fetch(executionEventsUrl(runId), {
          credentials: 'include',
          headers,
          signal: controller.signal,
        });
        if (response.status === 401) {
          window.dispatchEvent(new CustomEvent('execution:unauthorized'));
          controller.abort();
          return;
        }
        if (!response.ok || !response.body) {
          throw new Error(`SSE ${response.status}`);
        }

        const reconnecting = retryCount > 0;
        retryCount = 0;
        callbacksRef.current.onConnectionChange?.('connected');
        if (reconnecting) {
          callbacksRef.current.onReconnect?.();
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let shouldRotate = false;
        while (!controller.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) {
            throw new Error('SSE stream closed');
          }
          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split(/\r?\n\r?\n/);
          buffer = blocks.pop() || '';
          for (const block of blocks) {
            const event = parseExecutionEventBlock(block);
            if (!event) {
              continue;
            }
            if (event.id) {
              lastEventId = event.id;
            }
            callbacksRef.current.onEvent?.(event);
            if (event.type === 'stream.rotate') {
              shouldRotate = true;
              break;
            }
            if (event.type === 'stream.closed') {
              if (['authentication_required', 'session_invalid'].includes(event.data?.code)) {
                window.dispatchEvent(new CustomEvent('execution:unauthorized'));
              }
              callbacksRef.current.onConnectionChange?.('closed');
              await reader.cancel();
              return;
            }
          }
          if (shouldRotate) {
            await reader.cancel();
            const rotateError = new Error('SSE stream rotate');
            rotateError.reconnectImmediately = true;
            throw rotateError;
          }
        }
      } catch (error) {
        if (controller.signal.aborted) {
          return;
        }
        retryCount += 1;
        callbacksRef.current.onConnectionChange?.('reconnecting');
        const delay = error.reconnectImmediately
          ? 0
          : Math.min(1000 * (2 ** Math.min(retryCount - 1, 4)), 15000);
        retryTimer = window.setTimeout(connect, delay);
      }
    };

    connect();
    return () => {
      controller.abort();
      if (retryTimer) {
        window.clearTimeout(retryTimer);
      }
      callbacksRef.current.onConnectionChange?.('closed');
    };
  }, [enabled, runId]);
}
