import { createClientUuid } from './clientId';

const STORAGE_KEY = 'textile.execution.pending-run-requests.v1';
const MAX_PENDING_REQUESTS = 100;
const MAX_PENDING_AGE_MS = 24 * 60 * 60 * 1000;

const storage = () => {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
};

const loadPendingRequests = () => {
  const target = storage();
  if (!target) {
    return {};
  }
  try {
    const parsed = JSON.parse(target.getItem(STORAGE_KEY) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {};
    }
    const cutoff = Date.now() - MAX_PENDING_AGE_MS;
    return Object.fromEntries(
      Object.entries(parsed)
        .filter(([, entry]) => (
          entry
          && typeof entry.key === 'string'
          && entry.key.length > 0
          && Number(entry.created_at) >= cutoff
        ))
        .slice(-MAX_PENDING_REQUESTS),
    );
  } catch {
    target.removeItem(STORAGE_KEY);
    return {};
  }
};

const savePendingRequests = (requests) => {
  const target = storage();
  if (!target) {
    return;
  }
  try {
    target.setItem(STORAGE_KEY, JSON.stringify(requests));
  } catch {
    // sessionStorage 被浏览器策略禁用时仍可创建运行，只是不跨刷新复用。
  }
};

const normalizeForSignature = (value) => {
  if (Array.isArray(value)) {
    return value.map(normalizeForSignature);
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value)
        .filter(key => value[key] !== undefined)
        .sort()
        .map(key => [key, normalizeForSignature(value[key])]),
    );
  }
  return value;
};

const compactFingerprint = (value) => {
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    left = Math.imul(left ^ code, 0x01000193);
    right = Math.imul(right ^ (code + index), 0x85ebca6b);
  }
  return `v1:${value.length}:${(left >>> 0).toString(16)}:${(right >>> 0).toString(16)}`;
};

export const runRequestSignature = payload => compactFingerprint(
  JSON.stringify(normalizeForSignature({
    workflow_id: payload.workflow_id,
    inspection_number: payload.inspection_number?.trim(),
    input_data: payload.input_data || {},
    global_data: payload.global_data || {},
  })),
);

/**
 * 对尚未获得成功响应的同一份运行创建请求复用幂等键。
 *
 * 网络中断时，浏览器无法判断服务端是否已成功创建运行；保留该键可让
 * 用户再次点击时安全取得同一运行。收到成功响应后由调用方显式清除。
 */
export const prepareExecutionRunRequest = (payload) => {
  const signature = runRequestSignature(payload);
  const pendingRequests = loadPendingRequests();
  let idempotencyKey = pendingRequests[signature]?.key;
  if (!idempotencyKey) {
    idempotencyKey = createClientUuid();
    const entries = Object.entries(pendingRequests)
      .sort(([, left], [, right]) => Number(left.created_at) - Number(right.created_at));
    while (entries.length >= MAX_PENDING_REQUESTS) {
      const [oldestSignature] = entries.shift();
      delete pendingRequests[oldestSignature];
    }
    pendingRequests[signature] = {
      key: idempotencyKey,
      created_at: Date.now(),
    };
    savePendingRequests(pendingRequests);
  }
  return {
    signature,
    payload: {
      ...payload,
      idempotency_key: idempotencyKey,
    },
  };
};

export const clearExecutionRunRequest = signature => {
  const pendingRequests = loadPendingRequests();
  delete pendingRequests[signature];
  savePendingRequests(pendingRequests);
};

export const resetExecutionRunRequestCache = () => {
  storage()?.removeItem(STORAGE_KEY);
};
