export const saveInspectorName = (name) => {
  localStorage.setItem('inspector_name', name);
};

export const getInspectorName = () => {
  return localStorage.getItem('inspector_name') || '';
};

export const saveDeviceId = (deviceId) => {
  localStorage.setItem('selected_device_id', deviceId);
};

export const getDeviceId = () => {
  return localStorage.getItem('selected_device_id');
};
