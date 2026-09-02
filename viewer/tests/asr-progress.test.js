// Deterministic tests for the pure ASR-progress renderer shared by the feed
// card and the detail view. Run with: node --test tests/
//
// There is no browser test harness in this repo, so the renderer lives in its
// own dependency-free file (app/static/asr-progress.js) that both the PWA and
// node can load. Everything here is pure string-in / string-out: no DOM, no
// timers, no clock.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  asrProgressHtml,
  anyAsrActive,
  asrPercent,
} = require('../app/static/asr-progress.js');

const PROCESSING = {
  state: 'processing',
  total: 9,
  complete: 3,
  processing: 1,
  error: 0,
  pending: 5,
  percent: 33,
  label_ru: 'Расшифровывается · 3 из 9 блоков',
};
const ERRORED = {
  state: 'error',
  total: 4,
  complete: 1,
  processing: 0,
  error: 1,
  pending: 2,
  percent: 25,
  label_ru: 'Ошибка блока · повторится автоматически',
};
const PENDING = {
  state: 'pending',
  total: 6,
  complete: 0,
  processing: 0,
  error: 0,
  pending: 6,
  percent: 0,
  label_ru: 'Ожидает расшифровки',
};

test('renders nothing when there is no progress', () => {
  assert.equal(asrProgressHtml(null), '');
  assert.equal(asrProgressHtml(undefined), '');
  assert.equal(asrProgressHtml({}), '');
  assert.equal(asrProgressHtml('processing'), '');
  assert.equal(asrProgressHtml({ state: 'processing', total: 0 }), '');
});

test('processing state shows the Russian label and the percent', () => {
  const html = asrProgressHtml(PROCESSING);
  assert.match(html, /Расшифровывается · 3 из 9 блоков/);
  assert.match(html, /33%/);
  assert.match(html, /data-asr-state="processing"/);
});

test('progress bar is accessible', () => {
  const html = asrProgressHtml(PROCESSING);
  assert.match(html, /role="progressbar"/);
  assert.match(html, /aria-valuemin="0"/);
  assert.match(html, /aria-valuemax="100"/);
  assert.match(html, /aria-valuenow="33"/);
  assert.match(html, /aria-valuetext="33%"/);
  assert.match(html, /aria-label="Расшифровывается · 3 из 9 блоков"/);
});

test('bar width tracks the percent', () => {
  assert.match(asrProgressHtml(PROCESSING), /width:33%/);
  assert.match(asrProgressHtml(PENDING), /width:0%/);
});

test('error state keeps its own wording and marker', () => {
  const html = asrProgressHtml(ERRORED);
  assert.match(html, /Ошибка блока · повторится автоматически/);
  assert.match(html, /data-asr-state="error"/);
  assert.match(html, /25%/);
});

test('pending state announces that it is queued', () => {
  const html = asrProgressHtml(PENDING);
  assert.match(html, /Ожидает расшифровки/);
  assert.match(html, /data-asr-state="pending"/);
});

test('never invents an elapsed or remaining time', () => {
  for (const p of [PROCESSING, ERRORED, PENDING]) {
    const html = asrProgressHtml(p);
    for (const word of ['осталось', 'секунд', 'минут', 'часов', 'прошло', '~']) {
      assert.ok(!html.includes(word), `${p.state} leaked time word ${word}`);
    }
  }
});

test('renders no undefined/NaN placeholders', () => {
  for (const p of [PROCESSING, ERRORED, PENDING]) {
    const html = asrProgressHtml(p);
    assert.ok(!html.includes('undefined'), p.state);
    assert.ok(!html.includes('NaN'), p.state);
  }
});

test('falls back to a Russian label when the server sent none', () => {
  const html = asrProgressHtml({ state: 'processing', total: 3, percent: 0 });
  assert.ok(!html.includes('undefined'));
  assert.match(html, /[А-Яа-я]/);
});

test('escapes the label instead of trusting it as markup', () => {
  const html = asrProgressHtml({
    state: 'processing',
    total: 2,
    percent: 50,
    label_ru: '<img src=x onerror=alert(1)>"&',
  });
  assert.ok(!html.includes('<img'));
  assert.match(html, /&lt;img/);
  assert.match(html, /&quot;&amp;/);
});

test('percent is clamped to a whole 0..100', () => {
  assert.equal(asrPercent({ percent: -5 }), 0);
  assert.equal(asrPercent({ percent: 250 }), 100);
  assert.equal(asrPercent({ percent: 33.9 }), 33);
  assert.equal(asrPercent({ percent: 'nope' }), 0);
  assert.equal(asrPercent({}), 0);
  assert.equal(asrPercent(null), 0);
  assert.match(asrProgressHtml({ state: 'processing', total: 2, percent: 250 }),
    /aria-valuenow="100"/);
});

test('anyAsrActive drives polling and stops when nothing is running', () => {
  assert.equal(anyAsrActive([]), false);
  assert.equal(anyAsrActive(null), false);
  assert.equal(anyAsrActive('nope'), false);
  assert.equal(anyAsrActive([{ id: 'a', asr_processing: null }]), false);
  assert.equal(anyAsrActive([{ id: 'a' }, null, undefined]), false);
  assert.equal(anyAsrActive([{ id: 'a', asr_processing: PROCESSING }]), true);
  assert.equal(anyAsrActive([{ id: 'a', asr_processing: ERRORED }]), true);
  assert.equal(anyAsrActive([{ id: 'a', asr_processing: PENDING }]), true);
  assert.equal(
    anyAsrActive([{ asr_processing: null }, { asr_processing: PROCESSING }]),
    true);
  // a single object (the detail payload) is accepted too
  assert.equal(anyAsrActive({ id: 'a', asr_processing: PROCESSING }), true);
  assert.equal(anyAsrActive({ id: 'a', asr_processing: null }), false);
});
