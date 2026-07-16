export const queueRecordIdEquals = (left, right) => (
  left !== undefined
  && left !== null
  && right !== undefined
  && right !== null
  && String(left) === String(right)
);

export const getQueueSnapshotSignature = (queueList) => (Array.isArray(queueList) ? queueList : [])
  .map(record => `${record.id}:${record.position}:${record.version ?? ''}`)
  .join('|');

const recordMatchesDragDescriptor = (record, descriptor) => (
  record
  && queueRecordIdEquals(record.id, descriptor.recordId)
  && record.position === descriptor.recordPosition
  && String(record.version ?? '') === String(descriptor.recordVersion ?? '')
);

export const resolveStableQueueDrop = (queueList, dragDescriptor, targetDescriptor) => {
  if (
    !dragDescriptor
    || !targetDescriptor
    || dragDescriptor.recordId == null
    || targetDescriptor.recordId == null
  ) {
    return { ok: false, reason: 'invalid_drop' };
  }
  if (queueRecordIdEquals(dragDescriptor.recordId, targetDescriptor.recordId)) {
    return { ok: false, reason: 'same_record' };
  }

  const queueSnapshot = getQueueSnapshotSignature(queueList);
  if (
    dragDescriptor.queueSnapshot !== queueSnapshot
    || targetDescriptor.queueSnapshot !== queueSnapshot
  ) {
    return { ok: false, reason: 'queue_changed' };
  }

  const dragRecord = queueList.find(record => queueRecordIdEquals(record.id, dragDescriptor.recordId));
  const targetRecord = queueList.find(record => queueRecordIdEquals(record.id, targetDescriptor.recordId));
  if (
    !recordMatchesDragDescriptor(dragRecord, dragDescriptor)
    || !recordMatchesDragDescriptor(targetRecord, targetDescriptor)
  ) {
    return { ok: false, reason: 'queue_changed' };
  }

  return {
    ok: true,
    dragRecord,
    targetRecord,
    newPosition: targetRecord.position,
    queueSnapshot,
  };
};
