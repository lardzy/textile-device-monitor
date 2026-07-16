import assert from 'node:assert/strict';
import test from 'node:test';

import { getQueueSnapshotSignature, resolveStableQueueDrop } from '../src/utils/queueDrag.js';

const queue = [
  { id: 10, position: 1, version: 2, inspector_name: '甲' },
  { id: 11, position: 2, version: 4, inspector_name: '乙' },
  { id: 12, position: 3, version: 1, inspector_name: '丙' },
];

const descriptor = (record, snapshot = getQueueSnapshotSignature(queue)) => ({
  recordId: record.id,
  recordPosition: record.position,
  recordVersion: record.version,
  queueSnapshot: snapshot,
});

test('resolves a drop by stable record ids instead of array indexes', () => {
  const result = resolveStableQueueDrop(queue, descriptor(queue[2]), descriptor(queue[0]));

  assert.equal(result.ok, true);
  assert.equal(result.dragRecord.id, 12);
  assert.equal(result.targetRecord.id, 10);
  assert.equal(result.newPosition, 1);
});

test('rejects a drop when queue positions changed during dragging', () => {
  const drag = descriptor(queue[2]);
  const target = descriptor(queue[0]);
  const changed = [
    queue[0],
    { ...queue[2], position: 2, version: 2 },
    { ...queue[1], position: 3, version: 5 },
  ];

  assert.deepEqual(resolveStableQueueDrop(changed, drag, target), {
    ok: false,
    reason: 'queue_changed',
  });
});

test('rejects a drop when a record version changed without moving', () => {
  const drag = descriptor(queue[2]);
  const target = descriptor(queue[0]);
  const changed = queue.map(record => record.id === 12 ? { ...record, version: 2 } : record);

  assert.equal(resolveStableQueueDrop(changed, drag, target).ok, false);
});

