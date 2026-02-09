import api from './client';

export const resultsApi = {
  getLatest: (deviceId) => api.get('/results/latest', { params: { device_id: deviceId } }),
  getRecent: (deviceId, limit = 5) => api.get('/results/recent', { params: { device_id: deviceId, limit } }),
  getImages: (deviceId, params) => api.get('/results/images', { params: { device_id: deviceId, ...params } }),
  getTableUrl: (deviceId, folder) => {
    const folderParam = folder ? `&folder=${encodeURIComponent(folder)}` : '';
    return `/api/results/table?device_id=${deviceId}${folderParam}`;
  },
  getThumbUrl: (deviceId, filename, folder) => {
    const folderParam = folder ? `&folder=${encodeURIComponent(folder)}` : '';
    return `/api/results/thumb/${encodeURIComponent(filename)}?device_id=${deviceId}${folderParam}`;
  },
  getImageUrl: (deviceId, filename, folder) => {
    const folderParam = folder ? `&folder=${encodeURIComponent(folder)}` : '';
    return `/api/results/image/${encodeURIComponent(filename)}?device_id=${deviceId}${folderParam}`;
  },
  cleanupImages: (deviceId, folder, options = {}) => api.post('/results/cleanup', null, {
    params: {
      device_id: deviceId,
      ...(folder ? { folder } : {}),
      ...(options.renameEnabled != null ? { rename_enabled: options.renameEnabled } : {}),
      ...(options.newFolderName ? { new_folder_name: options.newFolderName } : {}),
    }
  }),
};
