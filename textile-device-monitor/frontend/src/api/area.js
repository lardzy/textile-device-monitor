import api from './client';

export const areaApi = {
  getConfig: () => api.get('/area/config'),
  updateConfig: (data) => api.put('/area/config', data),
  getArchiveStatus: () => api.get('/area/archive/status'),
  runArchive: () => api.post('/area/archive/run'),
  searchFolders: (params) => api.get('/area/folders/search', { params }),
  listRecentFolders: (params) => api.get('/area/folders/recent', { params }),
  listFolderImages: (folderName, params) => api.get(`/area/folders/${encodeURIComponent(folderName)}/images`, { params }),
  getFolderImageUrl: (folderName, filename) => `/api/area/folders/${encodeURIComponent(folderName)}/image/${encodeURIComponent(filename)}`,
  cleanupFolder: (folderName, data) => api.post(`/area/folders/${encodeURIComponent(folderName)}/cleanup`, data),
  createJob: (data) => api.post('/area/jobs', data),
  listJobs: (params) => api.get('/area/jobs', { params }),
  getJob: (jobId) => api.get(`/area/jobs/${encodeURIComponent(jobId)}`),
  getResult: (jobId) => api.get(`/area/jobs/${encodeURIComponent(jobId)}/result`),
  getImages: (jobId, params) => api.get(`/area/jobs/${encodeURIComponent(jobId)}/artifacts/images`, { params }),
  getEditorImages: (jobId, params) => api.get(`/area/jobs/${encodeURIComponent(jobId)}/editor/images`, { params }),
  getEditorImage: (jobId, imageId) => api.get(`/area/jobs/${encodeURIComponent(jobId)}/editor/images/${encodeURIComponent(imageId)}`),
  saveEditorImage: (jobId, imageId, data) => api.put(`/area/jobs/${encodeURIComponent(jobId)}/editor/images/${encodeURIComponent(imageId)}`, data),
  resetEditorImage: (jobId, imageId, data) => api.post(`/area/jobs/${encodeURIComponent(jobId)}/editor/images/${encodeURIComponent(imageId)}/reset`, data),
  getExcelUrl: (jobId) => `/api/area/jobs/${encodeURIComponent(jobId)}/artifacts/excel`,
  getImageUrl: (jobId, filename) => `/api/area/jobs/${encodeURIComponent(jobId)}/artifacts/image/${encodeURIComponent(filename)}`,
};
