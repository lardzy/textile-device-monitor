import api from './client';

export const ocrApi = {
  createJob: (formData) => api.post('/ocr/jobs', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  }),
  createBatchJobs: (formData) => api.post('/ocr/jobs/batch', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  }),
  getJob: (jobId) => api.get(`/ocr/jobs/${encodeURIComponent(jobId)}`),
  getJobResult: (jobId) => api.get(`/ocr/jobs/${encodeURIComponent(jobId)}/result`),
  exportBatchDocx: (jobIds) => api.post('/ocr/jobs/export/docx', {
    job_ids: jobIds,
  }, {
    responseType: 'blob',
  }),
  downloadArtifact: (jobId, kind) => api.get(`/ocr/jobs/${encodeURIComponent(jobId)}/artifacts/${kind}`, {
    responseType: 'blob',
  }),
};
