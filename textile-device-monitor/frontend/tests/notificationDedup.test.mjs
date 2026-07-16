import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildCompletionNoticeClaim,
  buildQueueTurnNoticeClaim,
  claimNotificationOnce,
} from '../src/utils/notificationDedup.js';

class MemoryStorage {
  constructor() {
    this.data = new Map();
  }

  getItem(key) {
    return this.data.has(key) ? this.data.get(key) : null;
  }

  setItem(key, value) {
    this.data.set(key, String(value));
  }
}

test('completion claim prefers report id and strong identities do not share a device lease', () => {
  const claim = buildCompletionNoticeClaim({
    device_id: 7,
    event_id: 'event-a',
    report_id: 'report-b',
    task_id: 'task-c',
  }, { now: 10_000 });

  assert.match(claim.entries[0].key, /report:report-b/);
  assert.equal(claim.entries.length, 1);
});

test('completion fallback includes task name and completion time bucket', () => {
  const claim = buildCompletionNoticeClaim({
    device_id: 9,
    task_name: '棉纤维检测',
    occurred_at: '2026-07-16T12:00:01.000Z',
  }, { now: Date.parse('2026-07-16T12:05:00.000Z'), recentWindowMs: 15_000 });

  assert.match(claim.entries[0].key, /fallback:%E6%A3%89%E7%BA%A4%E7%BB%B4%E6%A3%80%E6%B5%8B:/);
  assert.equal(claim.entries[1].key, 'completion:9:recent');
});

test('same report is claimed once while a different report is delivered immediately', async () => {
  const storage = new MemoryStorage();
  let now = 100_000;
  const first = buildCompletionNoticeClaim({ device_id: 1, report_id: 'r1' }, { now });

  assert.equal(await claimNotificationOnce(first, {
    storage,
    lockManager: null,
    owner: 'tab-a',
    now: () => now,
    settleMs: 0,
  }), true);
  assert.equal(await claimNotificationOnce(first, {
    storage,
    lockManager: null,
    owner: 'tab-b',
    now: () => now,
    settleMs: 0,
  }), false);

  const next = buildCompletionNoticeClaim({ device_id: 1, report_id: 'r2' }, { now });
  assert.equal(await claimNotificationOnce(next, {
    storage,
    lockManager: null,
    owner: 'tab-b',
    now: () => now,
    settleMs: 0,
  }), true);
});

test('queue turn claims use stable device and queue record ids', async () => {
  const storage = new MemoryStorage();
  const claim = buildQueueTurnNoticeClaim(3, 42);

  assert.equal(await claimNotificationOnce(claim, {
    storage,
    lockManager: null,
    owner: 'tab-a',
    now: 1_000,
    settleMs: 0,
  }), true);
  assert.equal(await claimNotificationOnce(claim, {
    storage,
    lockManager: null,
    owner: 'tab-b',
    now: 1_001,
    settleMs: 0,
  }), false);
});

test('a completion lease never suppresses the mandatory queue-turn lease', async () => {
  const storage = new MemoryStorage();
  const completion = buildCompletionNoticeClaim({ device_id: 3, report_id: 'r1' }, { now: 1_000 });
  const queueTurn = buildQueueTurnNoticeClaim(3, 42);

  assert.equal(await claimNotificationOnce(completion, {
    storage,
    lockManager: null,
    owner: 'completion-tab',
    now: 1_000,
    settleMs: 0,
  }), true);
  assert.equal(await claimNotificationOnce(queueTurn, {
    storage,
    lockManager: null,
    owner: 'queue-tab',
    now: 1_001,
    settleMs: 0,
  }), true);
});
