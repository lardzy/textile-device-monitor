import api from './client';

export const historyApi = {
  get: (params) => api.get('/history', { params }),
  export: (params) => api.get('/history/export', { params, responseType: 'blob' }),
  getByDevice: (deviceId) => api.get(`/history/device/${deviceId}`),
  getLatest: (deviceId) => api.get(`/history/latest/${deviceId}`),
};
