import axios from 'axios';

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  timeout: 600000, // 10 minutes for long-running OCR tasks (PaddleOCR-VL-1.5 initial processing)
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
    // Pass through the full error for better handling
    const status = error.response?.status;
    const message = error.response?.data?.detail || error.message || '请求失败';

    console.error('API Error:', message, 'Status:', status);

    // Create error with additional info
    const err = new Error(message);
    err.status = status;
    err.originalError = error;
    return Promise.reject(err);
  }
);

export default api;
