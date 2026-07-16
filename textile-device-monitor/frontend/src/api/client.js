import axios from 'axios';

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

api.interceptors.request.use(
  (config) => {
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

api.interceptors.response.use(
  (response) => {
    return response.data;
  },
  (error) => {
    const responseBody = error.response?.data;
    const detail = responseBody?.detail;
    const message =
      (typeof detail === 'string' ? detail : detail?.message || detail?.code)
      || responseBody?.message
      || responseBody?.code
      || (error.response?.status === 413 ? 'file_too_large' : null)
      || error.message
      || '请求失败';
    console.error('API Error:', message);
    const apiError = new Error(message);
    apiError.status = error.response?.status;
    apiError.detail = detail;
    apiError.code = responseBody?.code || detail?.code;
    apiError.body = responseBody;
    return Promise.reject(apiError);
  }
);

export default api;
