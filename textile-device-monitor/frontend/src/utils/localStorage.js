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

const QUEUE_NOTICE_MODE_KEY = 'queue_notice_mode_by_device';
const LEGACY_QUEUE_NOTICE_MODE_KEY = 'queue_notice_mode';
const QUEUE_NOTICE_ENTRIES_KEY = 'queue_notice_entries';
const QUEUE_USER_ID_KEY = 'queue_user_id';

const normalizeQueueId = (value) => {
  const numeric = Number(value);
  return Number.isNaN(numeric) ? value : numeric;
};

export const getQueueNoticeModes = () => {
  try {
    const raw = localStorage.getItem(QUEUE_NOTICE_MODE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (typeof parsed === 'string') {
        return { '*': parsed };
      }
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed;
      }
    }
  } catch (error) {
    // ignore parse errors
  }

  const legacy = localStorage.getItem(LEGACY_QUEUE_NOTICE_MODE_KEY);
  if (legacy) {
    return { '*': legacy };
  }
  return {};
};

export const saveQueueNoticeModes = (modes) => {
  localStorage.setItem(QUEUE_NOTICE_MODE_KEY, JSON.stringify(modes));
};

export const getQueueNoticeMode = (deviceId) => {
  const modes = getQueueNoticeModes();
  const key = deviceId != null ? String(deviceId) : '';
  return modes[key] || 'off';
};

export const saveQueueNoticeMode = (deviceId, mode) => {
  if (deviceId == null) return;
  const modes = getQueueNoticeModes();
  modes[String(deviceId)] = mode;
  saveQueueNoticeModes(modes);
};

export const getQueueNoticeEntries = () => {
  try {
    const raw = localStorage.getItem(QUEUE_NOTICE_ENTRIES_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    return [];
  }
};

export const saveQueueNoticeEntries = (entries) => {
  localStorage.setItem(QUEUE_NOTICE_ENTRIES_KEY, JSON.stringify(entries));
};

export const addQueueNoticeEntry = (entry) => {
  const entries = getQueueNoticeEntries();
  const normalizedEntry = { ...entry, id: normalizeQueueId(entry.id) };
  if (!entries.some(item => item.id === normalizedEntry.id)) {
    entries.push(normalizedEntry);
    saveQueueNoticeEntries(entries);
  }
  return entries;
};

export const removeQueueNoticeEntry = (queueId) => {
  const normalizedId = normalizeQueueId(queueId);
  const entries = getQueueNoticeEntries().filter(item => item.id !== normalizedId);
  saveQueueNoticeEntries(entries);
  return entries;
};

const generateQueueUserId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const seed = Math.random().toString(36).slice(2, 10);
  return `u_${Date.now().toString(36)}_${seed}`;
};

export const getQueueUserId = () => {
  return localStorage.getItem(QUEUE_USER_ID_KEY);
};

export const getOrCreateQueueUserId = () => {
  const existing = getQueueUserId();
  if (existing) return existing;
  const created = generateQueueUserId();
  localStorage.setItem(QUEUE_USER_ID_KEY, created);
  return created;
};
