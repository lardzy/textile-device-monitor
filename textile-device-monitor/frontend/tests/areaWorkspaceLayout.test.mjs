import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const workspaceSource = readFileSync(
  new URL('../src/pages/area/AreaJobWorkspace.jsx', import.meta.url),
  'utf8',
);
const areaCss = readFileSync(
  new URL('../src/pages/area/area.css', import.meta.url),
  'utf8',
);

const cssRule = (selector) => {
  const marker = `${selector} {`;
  const start = areaCss.indexOf(marker);
  assert.notEqual(start, -1, `missing CSS rule: ${selector}`);
  const end = areaCss.indexOf('}', start);
  assert.notEqual(end, -1, `unterminated CSS rule: ${selector}`);
  return areaCss.slice(start, end + 1);
};

test('area image rail gives the Spin wrapper a definite constrained height', () => {
  assert.match(
    workspaceSource,
    /<Spin\s+spinning=\{imagesLoading\}\s+wrapperClassName="area-image-list-spin">/,
  );

  const rail = cssRule('.area-image-rail');
  assert.match(rail, /grid-template-rows:\s*auto minmax\(0, 1fr\)/);

  const spin = cssRule('.area-image-list-spin');
  assert.match(spin, /height:\s*100%/);
  assert.match(spin, /min-height:\s*0/);
  assert.match(spin, /overflow:\s*hidden/);

  const spinContainer = cssRule('.area-image-list-spin > .ant-spin-container');
  assert.match(spinContainer, /height:\s*100%/);
  assert.match(spinContainer, /min-height:\s*0/);
  assert.match(spinContainer, /overflow:\s*hidden/);
});

test('area image list scrolls vertically without turning the page into the scroller', () => {
  const list = cssRule('.area-image-list');
  assert.match(list, /height:\s*100%/);
  assert.match(list, /min-height:\s*0/);
  assert.match(list, /overflow-x:\s*hidden/);
  assert.match(list, /overflow-y:\s*auto/);
  assert.match(list, /overscroll-behavior:\s*contain/);
});

test('area editor renders the backend semantic class names', () => {
  assert.match(workspaceSource, /detail\?\.class_names/);
  assert.match(
    workspaceSource,
    /<Select value=\{selectedInstance\.class_name\} options=\{classNames\.map/,
  );
  assert.match(workspaceSource, /<strong>\{item\.class_name\}<\/strong>/);
});

test('late image detail responses cannot overwrite the currently selected image', () => {
  assert.match(workspaceSource, /const detailRequestSeqRef = useRef\(0\)/);
  assert.match(
    workspaceSource,
    /shouldApplyAreaImageResponse\(\{[\s\S]*currentRequestSeq:\s*detailRequestSeqRef\.current[\s\S]*selectedImageId:\s*selectedImageIdRef\.current/,
  );
  assert.match(
    workspaceSource,
    /const selectImage[\s\S]*detailRequestSeqRef\.current \+= 1/,
  );
  assert.match(
    workspaceSource,
    /error\.status === 409[\s\S]*isCurrentEditingRequest\(\)/,
  );
  assert.match(
    workspaceSource,
    /const imagesRequestSeqRef = useRef\(0\)[\s\S]*isCurrentAreaRequest\(requestSeq, imagesRequestSeqRef\.current\)/,
  );
});
