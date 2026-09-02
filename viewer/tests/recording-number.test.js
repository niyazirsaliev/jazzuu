// The permanent recording code in the PWA: shown wherever a recording is, and
// copied by one tap.
//
// Like app-integration.test.js this evaluates the REAL app/static sources in a
// VM against the minidom stand-in — nothing here re-implements app logic. The
// browser APIs a copy button needs (clipboard, timers) are the only stubs, and
// the clipboard stub records exactly what was written so a test can prove that
// what lands there is the code and nothing else.
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
    name: 'Длинная запись',
    recording_number: 'N-0042',
    start_at: '2026-08-08 10:00:00',
    duration_ms: 3600000,
    lang: 'ru',
    summary: null,
    asr_processing: null,
  }, over);
}

function detailPayload(over = {}) {
  return Object.assign({
    id: 'rec1',
    name: 'Длинная запись',
    recording_number: 'N-0042',
    start_at: '2026-08-08 10:00:00',
    duration_ms: 3600000,
    lang: 'ru',
    engine: 'host',
    segments: [],
    asr_transcript: 'текст',
    plaud_transcript: null,
    transcript: 'текст',
    summary: null,
    summary_data: null,
    asr_route: {},
    asr_processing: null,
    has_summary_card: false,
    has_audio: false,
    has_mindmap: false,
  }, over);
}

// `clipboard: null` boots a browser that exposes no Clipboard API at all —
// an http:// origin, or an old iOS webview.
function boot(handler, opts = {}) {
  const doc = createDocument('<html><body><div id="app"></div></body></html>');
  const timers = new Map();
  let seq = 0;
  const written = [];
  const location = { hash: opts.hash || '#/' };
  const winListeners = new Map();
  const clipboard = opts.clipboard === null ? undefined : {
    writeText: (text) => {
      written.push(text);
      return opts.clipboardFails
        ? Promise.reject(new Error('denied'))
        : Promise.resolve();
    },
  };
  let selected = null;
  const createElement = doc.createElement.bind(doc);
  doc.createElement = (tag) => {
    const el = createElement(tag);
    if (String(tag).toLowerCase() === 'textarea') {
      el.select = () => { selected = el.value; };
      el.setSelectionRange = () => { selected = el.value; };
    }
    return el;
  };
  doc.execCommand = (command) => {
    if (command !== 'copy' || selected === null || opts.legacyCopyFails) return false;
    written.push(selected);
    return true;
  };

  const sandbox = {
    document: doc,
    location,
    console,
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    setInterval: () => { throw new Error('app must not use setInterval'); },
    fetch: (url) => Promise.resolve(handler(url)).then((payload) => ({
      ok: true, status: 200, json: () => Promise.resolve(payload),
    })),
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.scrollTo = () => {};
  sandbox.addEventListener = (type, fn) => {
    if (!winListeners.has(type)) winListeners.set(type, []);
    winListeners.get(type).push(fn);
  };
  sandbox.navigator = { serviceWorker: undefined, clipboard };

  const ctx = vm.createContext(sandbox);
  for (const name of SOURCES) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, name), 'utf8'), ctx,
      { filename: name });
  }

  return {
    doc,
    written,
    timers,
    location,
    html: () => doc.getElementById('app').innerHTML,
    bodyHtml: () => doc.body.innerHTML,
    flush: async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); },
    click(el) { el.dispatchEvent({ type: 'click' }); },
    runTimers: async () => {
      for (const [id, t] of [...timers.entries()]) { timers.delete(id); await t.fn(); }
    },
  };
}

const feedHandler = (rows) => (url) => {
  if (url.startsWith('/api/recordings?')) return rows;
  throw new Error('unexpected request ' + url);
};
const detailHandler = (payload) => (url) => {
  if (url.startsWith('/api/recordings/')) return payload;
  throw new Error('unexpected request ' + url);
};

test('the feed shows each recording its permanent code', async () => {
  const app = boot(feedHandler([feedRow(), feedRow({ id: 'rec2', recording_number: 'N-0043' })]));
  await app.flush();
  const html = app.html();
  assert.match(html, /N-0042/);
  assert.match(html, /N-0043/);
});

test('a recording with no code yet renders no empty chip', async () => {
  const app = boot(feedHandler([feedRow({ recording_number: null })]));
  await app.flush();
  assert.doesNotMatch(app.html(), /data-copy-number/);
  assert.doesNotMatch(app.html(), /null/);
});

test('the detail view offers a real, labelled button for the code', async () => {
  const app = boot(detailHandler(detailPayload()), { hash: '#/r/rec1' });
  await app.flush();
  const button = app.doc.querySelector('[data-copy-number]');
  assert.ok(button, 'no copy control rendered');
  // A <button> — not a div with a click handler — so Enter/Space and the tab
  // order come from the platform rather than from us.
  assert.equal(button.localName, 'button');
  assert.equal(button.getAttribute('type'), 'button');
  assert.equal(button.getAttribute('data-copy-number'), 'N-0042');
  assert.match(button.getAttribute('aria-label'), /N-0042/);
  assert.match(button.textContent, /N-0042/);
});

test('one tap copies the code and confirms it in Russian', async () => {
  const app = boot(detailHandler(detailPayload()), { hash: '#/r/rec1' });
  await app.flush();
  app.click(app.doc.querySelector('[data-copy-number]'));
  await app.flush();

  // The UI still shows the short spoken code; the clipboard adds context for MCP.
  assert.deepEqual(app.written, ['Recordings MCP recording: N-0042']);
  const status = app.doc.querySelector('[data-copy-status]');
  assert.ok(status, 'no confirmation shown beside the number');
  assert.equal(status.textContent.trim(), 'Скопировано');
  assert.equal(status.getAttribute('aria-live'), 'polite');
  assert.match(status.className, /ml-2/, 'confirmation is placed to the right of the number');
  assert.match(status.className, /text-emerald-300/, 'confirmation has an intentional readable success color');
  assert.doesNotMatch(status.className, /fixed|bottom-/, 'confirmation is not a detached bottom toast');
});

test('the confirmation clears itself', async () => {
  const app = boot(detailHandler(detailPayload()), { hash: '#/r/rec1' });
  await app.flush();
  app.click(app.doc.querySelector('[data-copy-number]'));
  await app.flush();
  assert.equal(app.doc.querySelector('[data-copy-status]').textContent.trim(), 'Скопировано');
  await app.runTimers();
  await app.flush();
  assert.equal(app.doc.querySelector('[data-copy-status]').textContent.trim(), '');
});

test('copying never navigates and never puts the code in the URL', async () => {
  const app = boot(detailHandler(detailPayload()), { hash: '#/r/rec1' });
  await app.flush();
  app.click(app.doc.querySelector('[data-copy-number]'));
  await app.flush();
  assert.equal(app.location.hash, '#/r/rec1');
});

test('an http origin with no Clipboard API copies through the legacy browser path', async () => {
  const app = boot(detailHandler(detailPayload()), { hash: '#/r/rec1', clipboard: null });
  await app.flush();
  app.click(app.doc.querySelector('[data-copy-number]'));
  await app.flush();
  assert.deepEqual(app.written, ['Recordings MCP recording: N-0042']);
  const status = app.doc.querySelector('[data-copy-status]');
  assert.ok(status, 'a successful copy must be confirmed');
  assert.equal(status.textContent.trim(), 'Скопировано');
});

test('a rejected clipboard write is reported, not swallowed', async () => {
  const app = boot(detailHandler(detailPayload()),
    { hash: '#/r/rec1', clipboardFails: true });
  await app.flush();
  app.click(app.doc.querySelector('[data-copy-number]'));
  await app.flush();
  const status = app.doc.querySelector('[data-copy-status]');
  assert.ok(status);
  assert.equal(status.textContent.trim(), 'Не скопировано');
});

test('a detail view for an unnumbered recording shows no copy control', async () => {
  const app = boot(detailHandler(detailPayload({ recording_number: null })),
    { hash: '#/r/rec1' });
  await app.flush();
  assert.equal(app.doc.querySelector('[data-copy-number]'), null);
});
