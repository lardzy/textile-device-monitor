import axios from 'axios';

const executionClient = axios.create({
  baseURL: import.meta.env.VITE_EXECUTION_API_URL || '/api/execution/v1',
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

let csrfToken = null;
let csrfPromise = null;

const isSafeMethod = (method = 'get') =>
  ['get', 'head', 'options'].includes(method.toLowerCase());

const loadCsrfToken = async () => {
  if (csrfToken) {
    return csrfToken;
  }
  if (!csrfPromise) {
    csrfPromise = executionClient
      .get('/auth/csrf', { skipCsrf: true })
      .then((payload) => {
        csrfToken = payload?.csrf_token || payload?.token || null;
        return csrfToken;
      })
      .finally(() => {
        csrfPromise = null;
      });
  }
  return csrfPromise;
};

executionClient.interceptors.request.use(async (config) => {
  if (!config.skipCsrf && !isSafeMethod(config.method)) {
    const token = await loadCsrfToken();
    if (token) {
      config.headers.set('X-CSRF-Token', token);
    }
  }
  return config;
});

executionClient.interceptors.response.use(
  response => response.data,
  (error) => {
    const body = error.response?.data;
    const detail = body?.detail;
    if (
      error.response?.status === 403
      && (body?.code === 'csrf_invalid' || detail?.code === 'csrf_invalid')
    ) {
      csrfToken = null;
      csrfPromise = null;
    }
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('execution:unauthorized'));
    }
    const message =
      body?.message
      || (typeof detail === 'string' ? detail : detail?.message)
      || detail?.code
      || body?.code
      || error.message
      || '请求失败';
    const apiError = new Error(message);
    apiError.status = error.response?.status;
    apiError.code = body?.code || detail?.code;
    apiError.details = body?.details || detail?.details || {};
    apiError.body = body;
    apiError.requestId = body?.request_id;
    throw apiError;
  },
);

export const resetExecutionCsrfToken = () => {
  csrfToken = null;
  csrfPromise = null;
};

export const getExecutionCsrfToken = loadCsrfToken;

export default executionClient;
