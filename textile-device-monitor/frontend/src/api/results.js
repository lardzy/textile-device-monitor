import api from './client';

export const resultsApi = {
  getLatest: (deviceId) => api.get('/results/latest', { params: { device_id: deviceId } }),
  getImages: (deviceId, params) => api.get('/results/images', { params: { device_id: deviceId, ...params } }),
  getTableUrl: (deviceId) => `/api/results/table?device_id=${deviceId}`,
  getImageUrl: (deviceId, filename, folder) => {
    const folderParam = folder ? `&folder=${encodeURIComponent(folder)}` : '';
    return `/api/results/image/${encodeURIComponent(filename)}?device_id=${deviceId}${folderParam}`;
  },
};
