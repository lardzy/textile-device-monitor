import api from './client';

export const queueApi = {
  getByDevice: (deviceId) => api.get(`/queue/${deviceId}`),
  join: (data) => api.post('/queue', data),
  updatePosition: (id, data) => api.put(`/queue/${id}/position`, data),
  leave: (id) => api.delete(`/queue/${id}`),
  complete: (deviceId) => api.post(`/queue/${deviceId}/complete`),
  getCount: (deviceId) => api.get(`/queue/count/${deviceId}`),
};
