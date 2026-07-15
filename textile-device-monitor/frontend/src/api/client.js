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
    const detail = error.response?.data?.detail;
    const message =
      (typeof detail === 'string' ? detail : detail?.code)
      || (error.response?.status === 413 ? 'file_too_large' : null)
      || error.message
      || '请求失败';
    console.error('API Error:', message);
    const apiError = new Error(message);
    apiError.status = error.response?.status;
    apiError.detail = detail;
    return Promise.reject(apiError);
  }
);

export default api;
