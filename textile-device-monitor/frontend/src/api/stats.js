import api from './client';

export const statsApi = {
  getRealtime: () => api.get('/stats/realtime'),
  getDeviceRealtime: (deviceId) => api.get(`/stats/device/${deviceId}`),
  getByDevice: (deviceId, params) => api.get(`/stats/devices/${deviceId}`, { params }),
  getSummary: (params) => api.get('/stats/summary', { params }),
};
