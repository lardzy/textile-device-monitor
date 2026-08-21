import axios from 'axios';

const executionV1BaseUrl = import.meta.env.VITE_EXECUTION_API_URL
  || '/api/execution/v1';
const executionV2BaseUrl = import.meta.env.VITE_EXECUTION_V2_API_URL
  || executionV1BaseUrl.replace(/\/v1\/?$/, '/v2');

const executionClient = axios.create({
  baseURL: executionV1BaseUrl,
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

export const executionV2Client = axios.create({
  baseURL: executionV2BaseUrl,
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

const protectMutation = async (config) => {
  if (!config.skipCsrf && !isSafeMethod(config.method)) {
    const token = await loadCsrfToken();
    if (token) {
      config.headers.set('X-CSRF-Token', token);
    }
  }
  return config;
};

const unwrapResponse = response => response.data;

const normalizeResponseError = (error) => {
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
};

const installInterceptors = (client) => {
  client.interceptors.request.use(protectMutation);
  client.interceptors.response.use(unwrapResponse, normalizeResponseError);
};

installInterceptors(executionClient);
installInterceptors(executionV2Client);

export const resetExecutionCsrfToken = () => {
  csrfToken = null;
  csrfPromise = null;
};

export const getExecutionCsrfToken = loadCsrfToken;

export default executionClient;
