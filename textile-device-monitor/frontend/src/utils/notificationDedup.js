const CLAIMS_STORAGE_KEY = 'notification_delivery_claims_v1';

export const COMPLETION_COALESCE_WINDOW_MS = 15_000;

const STRONG_COMPLETION_FIELDS = [
  ['report', ['report_id', 'reportId']],
  ['event', ['event_id', 'eventId']],
  ['task_id', ['task_id', 'taskId']],
  ['task_key', ['task_key', 'taskKey']],
  ['queue', ['queue_id', 'queueId']],
];

let memoryClaims = {};

const normalizeKeyPart = (value) => encodeURIComponent(String(value).trim().slice(0, 300));

const firstPresentValue = (source, keys) => {
  for (const key of keys) {
    const value = source?.[key];
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      return value;
    }
  }
  return null;
};

const parseEdgeTime = (source, fallbackNow) => {
  const raw = firstPresentValue(source, [
    'occurred_at',
    'occurredAt',
    'reported_at',
    'reportedAt',
    'completed_at',
    'completedAt',
  ]);
  const parsed = raw == null ? NaN : new Date(raw).getTime();
  return Number.isFinite(parsed) ? parsed : fallbackNow;
};

export const buildCompletionNoticeClaim = (source = {}, options = {}) => {
  const now = options.now ?? Date.now();
  const recentWindowMs = options.recentWindowMs ?? COMPLETION_COALESCE_WINDOW_MS;
  const deviceId = firstPresentValue(source, ['device_id', 'deviceId']);
  if (deviceId == null) return null;

  const devicePart = normalizeKeyPart(deviceId);
  let identity = null;
  for (const [label, fields] of STRONG_COMPLETION_FIELDS) {
    const value = firstPresentValue(source, fields);
    if (value != null) {
      identity = `${label}:${normalizeKeyPart(value)}`;
      break;
    }
  }

  const hasStrongIdentity = identity != null;
  if (!identity) {
    const taskName = firstPresentValue(source, ['task_name', 'taskName']) || 'unknown-task';
    const edgeBucket = Math.floor(parseEdgeTime(source, now) / recentWindowMs);
    identity = `fallback:${normalizeKeyPart(taskName)}:${edgeBucket}`;
  }

  const entries = [
    {
      key: `completion:${devicePart}:exact:${identity}`,
      ttlMs: hasStrongIdentity ? 10 * 60_000 : recentWindowMs * 2,
    },
  ];
  if (!hasStrongIdentity) {
    entries.push({
      // 旧事件没有稳定 ID 时，用短租约合并队列和进度两个来源。
      key: `completion:${devicePart}:recent`,
      ttlMs: recentWindowMs,
    });
  }

  return {
    lockKey: `completion:${devicePart}`,
    entries,
  };
};

export const buildQueueTurnNoticeClaim = (deviceId, queueId, ttlMs = 30_000) => {
  if (deviceId == null || queueId == null) return null;
  const key = `queue-turn:${normalizeKeyPart(deviceId)}:${normalizeKeyPart(queueId)}`;
  return {
    lockKey: key,
    entries: [{ key, ttlMs }],
  };
};

const getDefaultStorage = () => {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch (error) {
    return null;
  }
};

const getDefaultLockManager = () => {
  try {
    return typeof navigator !== 'undefined' ? navigator.locks : null;
  } catch (error) {
    return null;
  }
};

const generateOwner = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `notice_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
};

const readClaims = (storage, now) => {
  if (!storage) {
    return Object.fromEntries(
      Object.entries(memoryClaims).filter(([, value]) => Number(value?.expiresAt) > now)
    );
  }
  try {
    const parsed = JSON.parse(storage.getItem(CLAIMS_STORAGE_KEY) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(([, value]) => Number(value?.expiresAt) > now)
    );
  } catch (error) {
    return {};
  }
};

const writeClaims = (storage, claims) => {
  if (!storage) {
    memoryClaims = claims;
    return true;
  }
  try {
    storage.setItem(CLAIMS_STORAGE_KEY, JSON.stringify(claims));
    return true;
  } catch (error) {
    memoryClaims = claims;
    return false;
  }
};

const attemptClaim = (claim, storage, owner, now) => {
  const claims = readClaims(storage, now);
  if (claim.entries.some(entry => Number(claims[entry.key]?.expiresAt) > now)) {
    return { claimed: false, storage };
  }

  claim.entries.forEach(entry => {
    claims[entry.key] = {
      owner,
      claimedAt: now,
      expiresAt: now + Math.max(1, Number(entry.ttlMs) || 1),
    };
  });
  const usedStorage = writeClaims(storage, claims) ? storage : null;
  const persisted = readClaims(usedStorage, now);
  return {
    claimed: claim.entries.every(entry => persisted[entry.key]?.owner === owner),
    storage: usedStorage,
  };
};

const stillOwnsClaim = (claim, storage, owner, now) => {
  const claims = readClaims(storage, now);
  return claim.entries.every(entry => claims[entry.key]?.owner === owner);
};

export const claimNotificationOnce = async (claim, options = {}) => {
  if (!claim?.lockKey || !Array.isArray(claim.entries) || claim.entries.length === 0) {
    return false;
  }

  const storage = Object.prototype.hasOwnProperty.call(options, 'storage')
    ? options.storage
    : getDefaultStorage();
  const lockManager = Object.prototype.hasOwnProperty.call(options, 'lockManager')
    ? options.lockManager
    : getDefaultLockManager();
  const owner = options.owner || generateOwner();
  const getNow = typeof options.now === 'function' ? options.now : () => options.now ?? Date.now();

  if (lockManager?.request) {
    try {
      return await lockManager.request(
        `textile-monitor-notice:${claim.lockKey}`,
        { mode: 'exclusive' },
        () => attemptClaim(claim, storage, owner, getNow()).claimed
      );
    } catch (error) {
      // Fall back to a localStorage lease for browsers without a usable Web Locks implementation.
    }
  }

  const result = attemptClaim(claim, storage, owner, getNow());
  if (!result.claimed) return false;

  // localStorage has no atomic compare-and-set. A short settle/read-back makes
  // simultaneous fallback writers converge on the last owner in best effort.
  const settleMs = options.settleMs ?? (24 + Math.floor(Math.random() * 40));
  if (settleMs > 0) {
    await new Promise(resolve => setTimeout(resolve, settleMs));
  }
  return stillOwnsClaim(claim, result.storage, owner, getNow());
};
