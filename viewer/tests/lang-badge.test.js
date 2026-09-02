// The language badge in the feed: which languages a recording actually holds.
//
// Like recording-number.test.js this evaluates the REAL app/static sources in a
// VM against the minidom stand-in — nothing here re-implements app logic. The
// engine emits ISO codes ('ky'); the reader displays KG, and a
// genuinely bilingual recording reads as a pair rather than one opaque code.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./helpers/minidom.js');

const STATIC = path.join(__dirname, '..', 'app', 'static');
const SOURCES = ['asr-progress.js', 'app.js'];   // exactly index.html's order

function feedRow(over = {}) {
  return Object.assign({
    id: 'rec1',
    name: 'Запись',
    recording_number: 'N-0042',
    start_at: '2026-08-08 10:00:00',
    duration_ms: 600000,
    lang: 'ru',
    summary: null,
    asr_processing: null,
  }, over);
}

function boot(handler) {
  const doc = createDocument('<html><body><div id="app"></div></body></html>');
  const timers = new Map();
  let seq = 0;
  const location = { hash: '#/' };
  const winListeners = new Map();
  const sandbox = {
    document: doc,
    location,
    history: { replaceState() {}, pushState() {} },
    localStorage: {
      _v: new Map(),
      getItem(k) { return this._v.has(k) ? this._v.get(k) : null; },
      setItem(k, v) { this._v.set(k, String(v)); },
      removeItem(k) { this._v.delete(k); },
    },
    fetch: async (url) => ({
      ok: true, status: 200,
      json: async () => handler(String(url)),
      text: async () => JSON.stringify(handler(String(url))),
    }),
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: (fn) => { fn(); return 0; },
    scrollTo: () => {},
    console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = (type, fn) => {
    winListeners.set(type, [...(winListeners.get(type) || []), fn]);
  };
  sandbox.removeEventListener = () => {};
  sandbox.navigator = { serviceWorker: undefined, clipboard: undefined };

  const ctx = vm.createContext(sandbox);
  for (const name of SOURCES) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, name), 'utf8'), ctx,
      { filename: name });
  }
  return {
    html: () => doc.getElementById('app').innerHTML,
    flush: async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); },
  };
}

const feedHandler = (rows) => (url) => {
  if (url.startsWith('/api/recordings?')) return rows;
  throw new Error('unexpected request ' + url);
};

async function badgeFor(lang) {
  const app = boot(feedHandler([feedRow({ lang })]));
  await app.flush();
  const m = app.html().match(/rounded-md px-1\.5 py-0\.5">([^<]*)</);
  return m ? m[1] : null;
}

test('Kyrgyz reads as KG, not the ISO ky the engine emits', async () => {
  assert.equal(await badgeFor('ky'), 'KG');
});

test('a genuinely bilingual recording names both languages', async () => {
  // Real production rows: N-0213/N-0238 are ru+ky, N-0227 is en+ky.
  assert.equal(await badgeFor('ru+ky'), 'RU/KG');
  assert.equal(await badgeFor('en+ky'), 'EN/KG');
});

test('single languages keep their familiar labels', async () => {
  assert.equal(await badgeFor('ru'), 'RU');
  assert.equal(await badgeFor('en'), 'EN');
});

test('legacy rows with no per-window detail never render as MI', async () => {
  // The old code sliced 'mixed' to its first two letters and showed "MI".
  assert.equal(await badgeFor('mixed'), 'MIX');
});

test('a recording with no language renders no chip at all', async () => {
  const app = boot(feedHandler([feedRow({ lang: null })]));
  await app.flush();
  assert.doesNotMatch(app.html(), /rounded-md px-1\.5 py-0\.5/);
});
