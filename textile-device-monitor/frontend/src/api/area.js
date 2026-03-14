import api from './client';

export const areaApi = {
  getConfig: () => api.get('/area/config'),
  updateConfig: (data) => api.put('/area/config', data),
  createJob: (data) => api.post('/area/jobs', data),
  listJobs: (params) => api.get('/area/jobs', { params }),
  getJob: (jobId) => api.get(`/area/jobs/${encodeURIComponent(jobId)}`),
  getResult: (jobId) => api.get(`/area/jobs/${encodeURIComponent(jobId)}/result`),
  getImages: (jobId, params) => api.get(`/area/jobs/${encodeURIComponent(jobId)}/artifacts/images`, { params }),
  getExcelUrl: (jobId) => `/api/area/jobs/${encodeURIComponent(jobId)}/artifacts/excel`,
  getImageUrl: (jobId, filename) => `/api/area/jobs/${encodeURIComponent(jobId)}/artifacts/image/${encodeURIComponent(filename)}`,
};
