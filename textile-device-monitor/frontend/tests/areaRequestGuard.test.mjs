import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isCurrentAreaRequest,
  isSameAreaImage,
  shouldApplyAreaImageResponse,
} from '../src/pages/area/areaRequestGuard.js';

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

test('late detail response from image A cannot replace newer image B', async () => {
  let requestSeq = 0;
  let selectedImageId = 1;
  let appliedImageId = null;

  const load = async (imageId, request) => {
    const ownSeq = ++requestSeq;
    const payload = await request.promise;
    if (shouldApplyAreaImageResponse({
      requestSeq: ownSeq,
      currentRequestSeq: requestSeq,
      requestedImageId: imageId,
      selectedImageId,
    })) {
      appliedImageId = payload.imageId;
    }
  };

  const requestA = deferred();
  const requestB = deferred();
  const loadingA = load(1, requestA);
  selectedImageId = 2;
  const loadingB = load(2, requestB);

  requestB.resolve({ imageId: 2 });
  await loadingB;
  requestA.resolve({ imageId: 1 });
  await loadingA;

  assert.equal(appliedImageId, 2);
});

test('save and reset responses are ignored after switching images', () => {
  assert.equal(isSameAreaImage(7, '7'), true);
  assert.equal(isSameAreaImage(7, 8), false);
});

test('an old save response is ignored after switching A to B and back to A', async () => {
  let currentRequestSeq = 10;
  let selectedImageId = 1;
  let appliedVersion = null;
  const oldSave = deferred();

  const applySaveResponse = async () => {
    const payload = await oldSave.promise;
    if (shouldApplyAreaImageResponse({
      requestSeq: 10,
      currentRequestSeq,
      requestedImageId: 1,
      selectedImageId,
    })) {
      appliedVersion = payload.version;
    }
  };

  const pendingSave = applySaveResponse();
  currentRequestSeq += 1;
  selectedImageId = 2;
  currentRequestSeq += 1;
  selectedImageId = 1;
  oldSave.resolve({ version: 2 });
  await pendingSave;

  assert.equal(appliedVersion, null);
});

test('an old image-list response cannot replace a newer filter response', async () => {
  let currentRequestSeq = 0;
  let appliedFilter = null;
  const allRequest = deferred();
  const failedRequest = deferred();

  const load = async (filter, request) => {
    const requestSeq = ++currentRequestSeq;
    await request.promise;
    if (isCurrentAreaRequest(requestSeq, currentRequestSeq)) {
      appliedFilter = filter;
    }
  };

  const loadingAll = load('all', allRequest);
  const loadingFailed = load('failed', failedRequest);
  failedRequest.resolve();
  await loadingFailed;
  allRequest.resolve();
  await loadingAll;

  assert.equal(appliedFilter, 'failed');
});
