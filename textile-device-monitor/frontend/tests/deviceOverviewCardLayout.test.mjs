import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const monitorSource = readFileSync(
  new URL('../src/pages/DeviceMonitor.jsx', import.meta.url),
  'utf8',
);
const monitorCss = readFileSync(
  new URL('../src/pages/device-monitor.css', import.meta.url),
  'utf8',
);

const cardStart = monitorSource.indexOf('const DeviceOverviewCard');
const cardEnd = monitorSource.indexOf("const type = 'queue-row'", cardStart);
const cardSource = monitorSource.slice(cardStart, cardEnd);

const cssRule = (selector) => {
  const marker = `${selector} {`;
  const start = monitorCss.indexOf(marker);
  assert.notEqual(start, -1, `missing CSS rule: ${selector}`);
  const end = monitorCss.indexOf('}', start);
  assert.notEqual(end, -1, `unterminated CSS rule: ${selector}`);
  return monitorCss.slice(start, end + 1);
};

test('device overview cards keep only location and model as secondary metadata', () => {
  assert.match(cardSource, /<small>位置<\/small>/);
  assert.match(cardSource, /<small>型号<\/small>/);
  assert.doesNotMatch(cardSource, /<small>(?:心跳|离线于|温度)<\/small>/);
  assert.doesNotMatch(cardSource, /offline_last_seen|metrics\?\.temperature/);
});

test('device overview cards use a compact neutral container without a status side stripe', () => {
  assert.doesNotMatch(monitorCss, /\.monitor-device-card::before/);
  assert.doesNotMatch(monitorCss, /--monitor-status-accent/);

  const card = cssRule('.monitor-device-card');
  assert.match(card, /min-height:\s*216px/);
  assert.match(card, /border:\s*1px solid #d8dee8/);

  const stateCopy = cssRule('.monitor-device-card__state-copy');
  assert.match(stateCopy, /min-height:\s*54px/);
});
