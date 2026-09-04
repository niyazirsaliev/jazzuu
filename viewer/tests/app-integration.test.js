// Integration test: the REAL app/static/asr-progress.js and app/static/app.js,
// evaluated in a VM against the minidom browser stand-in.
//
// The other JS tests require() the helper module and check it in isolation.
// That cannot catch the failures that actually break the installed PWA — a
// helper the app never calls, a `asrPoller` referenced before it exists, a
// second visibility listener, a refresh that rips the <audio> element out of
// the page mid-playback. So this file loads the production sources verbatim,
// in the order index.html loads them, drives them through the real fetch/timer
// entry points, and asserts on the resulting DOM.
//
// Nothing here re-implements app logic: fetch, timers, visibility and
// location.hash are the only things stubbed, and each stub is a browser API,
// not a piece of the app.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./helpers/minidom.js');

const STATIC = path.join(__dirname, '..', 'app', 'static');
const SOURCES = ['asr-progress.js', 'app.js'];   // exactly index.html's order

function progress(over = {}) {
  return Object.assign({
    state: 'processing',
    total: 9,
    complete: 3,
    processing: 1,
    error: 0,
    pending: 5,
    percent: 33,
    label_ru: 'Расшифровывается · 3 из 9 блоков',
  }, over);
}

function feedRow(over = {}) {
  return Object.assign({
    id: 'rec1',
    name: 'Длинная запись',
    start_at: '2026-08-08 10:00:00',
    duration_ms: 3600000,
    lang: 'ru',
    summary: null,
    asr_processing: progress(),
  }, over);
}

function detailPayload(over = {}) {
  return Object.assign({
    id: 'rec1',
    name: 'Длинная запись',
    start_at: '2026-08-08 10:00:00',
    duration_ms: 3600000,
    lang: 'ru',
    engine: 'host',
    segments: [],
    asr_transcript: null,
    plaud_transcript: null,
    transcript: '',
    summary: null,
    summary_data: null,
    asr_route: {},
    asr_processing: progress(),
    has_summary_card: false,
    has_audio: true,
    has_mindmap: false,
  }, over);
}

// Boot the real app in a VM. `handler(url)` returns the payload for a request;
// returning a promise lets a test hold a response open mid-flight.
function boot(handler, opts = {}) {
  const doc = createDocument('<html><body><div id="app"></div></body></html>');
  const timers = new Map();
  let seq = 0;
  const requests = [];
  const fetchCalls = [];
  const clipboardWrites = [];
  const errors = [];
  const location = { hash: opts.hash || '#/' };
  const winListeners = new Map();

  let handlerRef = handler;
  const storage = new Map(Object.entries(opts.storage || {}));
  const sandbox = {
    document: doc,
    localStorage: { getItem: (key) => storage.has(key) ? storage.get(key) : null, setItem: (key, value) => storage.set(key, String(value)) },
    location,
    console,
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    setInterval: () => { throw new Error('app must not use setInterval'); },
    fetch: (url, options) => {
      requests.push(url);
      fetchCalls.push({ url, options: options || {} });
      return Promise.resolve(handlerRef(url, options)).then((payload) => {
        if (payload && payload.__status) {
          return {
            ok: false,
            status: payload.__status,
            json: () => Promise.resolve(payload.__body || null),
          };
        }
        return { ok: true, status: 200, json: () => Promise.resolve(payload) };
      });
    },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.scrollTo = () => {};
  sandbox.addEventListener = (type, fn) => {
    if (!winListeners.has(type)) winListeners.set(type, []);
    winListeners.get(type).push(fn);
  };
  sandbox.navigator = {
    serviceWorker: undefined,
    clipboard: {
      writeText: async (text) => {
        if (opts.clipboardWrite) return opts.clipboardWrite(text);
        clipboardWrites.push(text);
      },
    },
  };

  const ctx = vm.createContext(sandbox);
  for (const name of SOURCES) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, name), 'utf8'), ctx,
      { filename: name });
  }

  const api = {
    get handler() { return handlerRef; },
    set handler(value) { handlerRef = value; },
    tap(el) {
      assert.ok(el, 'tap target exists');
      el.dispatchEvent({ type: 'pointerdown', pointerType: 'touch', isPrimary: true });
      el.dispatchEvent({ type: 'pointerup', pointerType: 'touch', isPrimary: true });
      el.dispatchEvent({ type: 'mousedown' });
      el.dispatchEvent({ type: 'mouseup' });
      el.dispatchEvent({ type: 'click', detail: 1 });
    },
    doc,
    ctx,
    timers,
    requests,
    fetchCalls,
    clipboardWrites,
    errors,
    location,
    appEl: () => doc.getElementById('app'),
    html: () => doc.getElementById('app').innerHTML,
    slots: () => doc.querySelectorAll('[data-asr-id]'),
    evalIn: (expr) => vm.runInContext(expr, ctx),
    // let queued promise jobs run
    flush: async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); },
    pending: () => [...timers.values()],
    async fireTimer() {
      assert.equal(timers.size, 1, 'expected exactly one pending timer');
      const [id, t] = [...timers.entries()][0];
      timers.delete(id);
      await t.fn();
      await api.flush();
    },
    setVisibility(state) {
      doc.visibilityState = state;
      doc.dispatchEvent({ type: 'visibilitychange' });
    },
    async navigate(hash) {
      location.hash = hash;
      for (const fn of winListeners.get('hashchange') || []) fn();
      await api.flush();
    },
    visibilityListeners: () => (doc.listeners.get('visibilitychange') || []).length,
  };
  return api;
}

function feedHandler(rowsRef) {
  return (url) => {
    if (url.startsWith('/api/recordings?')) return rowsRef.rows;
    throw new Error('unexpected request ' + url);
  };
}

test('the production shell boots with no ReferenceError and wires one poller', async () => {
  const rowsRef = { rows: [feedRow()] };
  const app = boot(feedHandler(rowsRef));
  await app.flush();

  // asr-progress.js took its browser-global branch and app.js could see it
  assert.equal(app.evalIn('typeof asrProgressHtml'), 'function');
  assert.equal(app.evalIn('typeof createAsrPoller'), 'function');
  // the poller app.js actually uses exists and is a single module-scope value
  assert.equal(app.evalIn('typeof asrPoller'), 'object');
  assert.equal(app.evalIn('typeof asrPoller.note'), 'function');
  assert.equal(app.evalIn('asrPoller.isActive()'), true);
  assert.equal(app.evalIn('asrPoller.isArmed()'), true);
});

test('the feed renders real progress markup into the card slot', async () => {
  const rowsRef = { rows: [feedRow()] };
  const app = boot(feedHandler(rowsRef));
  await app.flush();

  const slots = app.slots();
  assert.equal(slots.length, 1);
  const html = slots[0].innerHTML;
  assert.match(html, /Расшифровывается · 3 из 9 блоков/);
  assert.match(html, /role="progressbar"/);
  assert.match(html, /aria-valuenow="33"/);
});

test('exactly one 15s timer is armed, and never a second one', async () => {
  const rowsRef = { rows: [feedRow()] };
  const app = boot(feedHandler(rowsRef));
  await app.flush();
  assert.equal(app.timers.size, 1);
  assert.equal(app.pending()[0].ms, 15000);

  await app.fireTimer();                 // a full poll cycle
  assert.equal(app.timers.size, 1, 'still exactly one after a tick');
  await app.navigate('#/');              // re-route over the same view
  assert.equal(app.timers.size, 1, 'navigation must not stack timers');
});

test('one visibilitychange listener; hidden disarms, visible resumes', async () => {
  const rowsRef = { rows: [feedRow()] };
  const app = boot(feedHandler(rowsRef));
  await app.flush();
  assert.equal(app.visibilityListeners(), 1, 'exactly one listener for the page');

  app.setVisibility('hidden');
  assert.equal(app.timers.size, 0, 'a hidden tab must not hold a poll timer');
  const before = app.requests.length;

  app.setVisibility('visible');
  await app.flush();
  assert.ok(app.requests.length > before, 'returning refreshes immediately');
  assert.equal(app.timers.size, 1);

  // re-routing does not accumulate listeners either
  await app.navigate('#/');
  assert.equal(app.visibilityListeners(), 1);
});

test('a poll patches the slot in place without rebuilding the card', async () => {
  const rowsRef = { rows: [feedRow()] };
  const app = boot(feedHandler(rowsRef));
  await app.flush();

  const slotBefore = app.slots()[0];
  const cardBefore = app.doc.querySelector('button');

  rowsRef.rows = [feedRow({
    asr_processing: progress({ complete: 7, pending: 1, percent: 77,
      label_ru: 'Расшифровывается · 7 из 9 блоков' }),
  })];
  await app.fireTimer();

  const slotAfter = app.slots()[0];
  assert.equal(slotAfter, slotBefore, 'the slot element itself must survive');
  assert.equal(app.doc.querySelector('button'), cardBefore, 'card not rebuilt');
  assert.match(slotAfter.innerHTML, /Расшифровывается · 7 из 9 блоков/);
  assert.match(slotAfter.innerHTML, /aria-valuenow="77"/);
});

test('a finished transcript clears the badge and stops the loop', async () => {
  const rowsRef = { rows: [feedRow()] };
  const app = boot(feedHandler(rowsRef));
  await app.flush();
  assert.notEqual(app.slots()[0].innerHTML, '');

  rowsRef.rows = [feedRow({ asr_processing: null })];
  await app.fireTimer();

  assert.equal(app.slots()[0].innerHTML, '', 'progress replaced by nothing');
  assert.equal(app.evalIn('asrPoller.isActive()'), false);
  assert.equal(app.timers.size, 0, 'polling stops when nothing is running');
});

test('a detail poll never replaces the audio element', async () => {
  const state = { detail: detailPayload() };
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url.startsWith('/api/recordings/')) return state.detail;
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');

  const audioBefore = app.doc.getElementById('rec-audio');
  assert.ok(audioBefore, 'detail rendered its player');
  assert.equal(app.evalIn('_route.kind'), 'detail');

  // progress advances -> badge patched, player untouched
  state.detail = detailPayload({
    asr_processing: progress({ complete: 8, percent: 88,
      label_ru: 'Расшифровывается · 8 из 9 блоков' }),
  });
  await app.fireTimer();
  assert.equal(app.doc.getElementById('rec-audio'), audioBefore,
    'the <audio> element must be the same node — replacing it stops playback');
  assert.match(app.doc.querySelector('[data-asr-id]').innerHTML, /8 из 9/);

  // transcript lands -> body rebuilt, player STILL the same node
  state.detail = detailPayload({
    asr_transcript: 'Готовая расшифровка целиком.',
    asr_processing: null,
  });
  await app.fireTimer();
  assert.equal(app.doc.getElementById('rec-audio'), audioBefore,
    'rebuilding #detail-body must not touch the player');
  assert.match(app.html(), /Готовая расшифровка целиком/);
});

test('detail exposes a dedicated audio download link', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url.startsWith('/api/recordings/')) return detailPayload();
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');

  assert.match(app.html(), /Скачать аудио/);
  assert.match(app.html(), /href="\/audio\/rec1\?download=1"/);
  assert.match(app.html(), /download="rec1\.mp3"/);
  const audio = app.doc.getElementById('rec-audio');
  assert.ok(audio, 'player remains available');
  assert.doesNotMatch(audio.parentNode.className, /sticky|top-14/, 'audio block scrolls with the recording');
});

test('archive navigation stays in the header while infrequent detail actions follow all content', async () => {
  const state = { detail: detailPayload({
    archived: false,
    public_share_enabled: true,
    public_shares: [],
  }) };
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url.startsWith('/api/recordings/')) return state.detail;
    throw new Error('unexpected request ' + url);
  });
  await app.flush();

  const archiveNav = app.doc.querySelector('[aria-label="Архив"]');
  assert.ok(archiveNav, 'feed header exposes archive navigation');
  assert.equal(archiveNav.textContent.trim(), '', 'compact archive navigation is icon-only');
  assert.equal(archiveNav.getAttribute('title'), 'Архив');
  assert.match(archiveNav.className, /min-w-11/);
  assert.match(archiveNav.className, /min-h-11/, 'archive navigation has a mobile-sized target');

  await app.navigate('#/r/rec1');
  let html = app.html();
  const actions = app.doc.querySelector('[data-detail-actions]');
  assert.ok(actions, 'detail has one low-emphasis actions area');
  assert.equal((html.match(/data-detail-actions/g) || []).length, 1);
  assert.equal((html.match(/data-archive-action=/g) || []).length, 1, 'detail has one archive action');
  assert.match(actions.textContent, /Публичная ссылка/);
  assert.match(actions.textContent, /Заархивировать запись/);
  assert.match(actions.textContent, /Скачать аудио/);
  assert.ok(html.indexOf('data-comments') < html.indexOf('data-detail-actions'),
    'tabs, normal detail content, and comments precede infrequent actions');
  assert.ok(html.indexOf('id="public-share-slot"') > html.indexOf('data-detail-actions'),
    'public-share management is inside the bottom actions area');
  assert.ok(html.indexOf('data-archive-action=') > html.indexOf('data-detail-actions'),
    'archive action is inside the bottom actions area');
  assert.ok(html.indexOf('?download=1') > html.indexOf('data-detail-actions'),
    'audio download is inside the bottom actions area');
  assert.equal(app.doc.getElementById('rec-audio').parentNode.querySelector('a'), null,
    'the player remains near the top without the download link attached');
  assert.equal(app.doc.querySelector('[data-recording-labels]'), null,
    'detail omits label assignment controls');

  state.detail = detailPayload({
    archived: true,
    public_share_enabled: true,
    public_shares: [],
  });
  await app.navigate('#/r/rec1');
  html = app.html();
  assert.match(html, /Запись в архиве/, 'archived detail clearly identifies its state');
  assert.match(html, /data-archive-action="restore"/);
  assert.match(html, /Вернуть/);
  assert.match(html, /data-delete-local/);
  assert.match(html, /text-red-300/, 'local deletion remains visually destructive');
  assert.ok(html.indexOf('data-comments') < html.indexOf('data-archive-action="restore"'),
    'restore and delete remain at the bottom after comments');
  assert.equal((html.match(/data-archive-action=/g) || []).length, 1, 'archived detail has no duplicate controls');
  assert.equal((html.match(/data-delete-local/g) || []).length, 1, 'delete is only rendered once for archived detail');
});

test('feed hides label filter controls while retaining label data', async () => {
  const definitions = [
    { id: 'personal', name: 'Личное', kind: 'system' },
    { id: 'business', name: 'Работа', kind: 'system' },
  ];
  const app = boot(feedHandler({ rows: [
    feedRow({ labels: ['personal'], label_definitions: definitions }),
  ] }));
  await app.flush();

  const html = app.html();
  assert.doesNotMatch(html, /aria-label="Фильтры"/);
  assert.doesNotMatch(html, /data-label-filter=/);
  assert.doesNotMatch(html, /data-label-catalog-(?:create|edit)/);
  assert.match(html, /Личное/, 'assigned label data remains visible on the recording card');
});

test('recording detail hides label assignment controls', async () => {
  const definitions = [
    { id: 'personal', name: 'Личное', kind: 'system' },
    { id: 'business', name: 'Работа', kind: 'system' },
  ];
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow({
      labels: ['personal'], label_definitions: definitions,
    })];
    if (url.startsWith('/api/recordings/')) return detailPayload({
      labels: ['personal', 'business'], label_definitions: definitions,
    });
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');

  const html = app.html();
  assert.doesNotMatch(html, /data-recording-labels/);
  assert.doesNotMatch(html, /Новые фильтры создаются/);
  assert.equal(app.doc.querySelector('[data-label-id]'), null);
});

test('the open detail survives a poll and keeps its tab', async () => {
  const state = { detail: detailPayload() };
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url.startsWith('/api/recordings/')) return state.detail;
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');
  app.evalIn("showTab('transcript')");
  assert.equal(app.evalIn('_detailTab'), 'transcript');

  state.detail = detailPayload({ asr_processing: progress({ percent: 44 }) });
  await app.fireTimer();

  assert.equal(app.evalIn('_route.kind'), 'detail', 'still on the same recording');
  assert.equal(app.evalIn('_route.id'), 'rec1');
  assert.equal(app.evalIn('_detailTab'), 'transcript', 'reader\'s tab preserved');
  assert.match(app.html(), /Длинная запись/);
});

test('a stale detail response cannot repaint a view the reader left', async () => {
  let release;
  const held = new Promise((res) => { release = res; });
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow({ name: 'Лента' })];
    if (url.startsWith('/api/recordings/')) return held;
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');   // detail request is now hanging

  // reader goes back before the detail response arrives
  await app.navigate('#/');
  const feedHtml = app.html();
  assert.match(feedHtml, /Лента/);

  release(detailPayload({ name: 'Опоздавшая запись' }));
  await app.flush();

  assert.equal(app.evalIn('_route.kind'), 'feed');
  assert.ok(!app.html().includes('Опоздавшая запись'),
    'a late detail response must not paint over the feed');
  assert.match(app.html(), /Лента/);
});

test('a transient poll failure keeps the loop alive; a 401 kills it', async () => {
  const mode = { fail: false, unauth: false };
  const rows = [feedRow()];
  const app = boot((url) => {
    if (mode.unauth) return { __status: 401 };
    if (mode.fail) return { __status: 502 };
    if (url.startsWith('/api/recordings?')) return rows;
    throw new Error('unexpected request ' + url);
  });
  await app.flush();

  mode.fail = true;
  await app.fireTimer();
  assert.equal(app.timers.size, 1, 'a 502 must not end the refresh loop');

  mode.fail = false;
  mode.unauth = true;
  await app.fireTimer();
  assert.equal(app.timers.size, 0, 'the auth wall ends it');
  assert.equal(app.evalIn('asrPoller.isActive()'), false);
  assert.match(app.html(), /Требуется вход/);
});

test('the search screen is not polled', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url.startsWith('/api/search')) return [];
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  assert.equal(app.timers.size, 1);

  await app.navigate('#/search');
  assert.equal(app.timers.size, 0, 'nothing on the search screen reports progress');
  assert.equal(app.evalIn('asrPoller.isActive()'), false);
});

test('search input debounces incremental encoded requests and clears on backspace', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url.startsWith('/api/search')) return [];
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/search');
  const input = app.doc.getElementById('q');

  input.value = 'НИЯ';
  input.dispatchEvent({ type: 'input' });
  assert.equal(app.timers.size, 2);
  assert.deepEqual(app.pending().map(t => t.ms).sort((a, b) => a - b), [220, 450]);

  input.value = 'НИ & план';
  input.dispatchEvent({ type: 'input' });
  assert.equal(app.timers.size, 2, 'typing replaces both prior debounces');
  for (const [id, timer] of [...app.timers.entries()].sort((a, b) => a[1].ms - b[1].ms)) {
    app.timers.delete(id);
    await timer.fn();
    await app.flush();
  }
  assert.ok(app.requests.includes('/api/search?q=%D0%9D%D0%98%20%26%20%D0%BF%D0%BB%D0%B0%D0%BD'));
  assert.ok(app.requests.includes('/api/search?q=%D0%9D%D0%98%20%26%20%D0%BF%D0%BB%D0%B0%D0%BD&semantic=true'));

  input.value = '';
  input.dispatchEvent({ type: 'input' });
  assert.equal(app.timers.size, 0, 'clearing cancels both searches instead of retaining output');
  assert.equal(app.doc.getElementById('results').innerHTML, '');
});

test('an older same-query response cannot overwrite the newest search results', async () => {
  let releaseOld;
  const held = new Promise((resolve) => { releaseOld = resolve; });
  let aRequests = 0;
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url === '/api/search?q=A') {
      aRequests += 1;
      if (aRequests === 1) return held;
      return [{ id: 'new', name: 'Новый ответ', snippet: 'новый' }];
    }
    if (url === '/api/search?q=B') return [];
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/search');
  const input = app.doc.getElementById('q');

  input.value = 'A';
  input.dispatchEvent({ type: 'input' });
  const [oldTimerId, oldTimer] = [...app.timers.entries()][0];
  app.timers.delete(oldTimerId);
  oldTimer.fn();
  await app.flush();

  input.value = 'B';
  input.dispatchEvent({ type: 'input' });
  await app.fireTimer();
  input.value = 'A';
  input.dispatchEvent({ type: 'input' });
  await app.fireTimer();
  assert.match(app.html(), /Новый ответ/);

  releaseOld([{ id: 'old', name: 'Старый ответ', snippet: 'старый' }]);
  await app.flush();

  assert.match(app.html(), /Новый ответ/);
  assert.ok(!app.html().includes('Старый ответ'));
});

test('the image overlay still closes only via its X button', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    if (url.startsWith('/api/recordings/')) return detailPayload({
      has_mindmap: true,
      mindmap_revision: 'ru-summary-rev',
    });
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');

  const trigger = app.doc.querySelector('[data-mm-open]');
  assert.ok(trigger, 'the real detail view renders the map trigger');
  const inlineMap = trigger.querySelector('img');
  assert.equal(
    inlineMap.getAttribute('src'),
    '/api/recordings/rec1/mindmap.png?v=ru-summary-rev',
    'the inline map URL changes with the structured Russian summary',
  );
  assert.match(inlineMap.className, /max-h-\[360px\]/, 'inline preview is height-bounded on mobile');
  assert.match(inlineMap.className, /md:max-h-\[480px\]/, 'inline preview is height-bounded on desktop');
  assert.match(inlineMap.className, /object-contain/, 'the bounded preview preserves the whole map');
  trigger.focus();
  const background = app.doc.getElementById('app');
  background.setAttribute('aria-hidden', 'legacy');
  background.setAttribute('inert', 'legacy');
  background.inert = true;
  app.doc.body.style.overflow = 'scroll';

  app.evalIn("openMindmap('rec1')");
  const overlay = app.doc.getElementById('mm-overlay');
  assert.ok(overlay, 'overlay opened');
  const close = overlay.querySelector('[data-mm-close]');
  assert.ok(close, 'the fixed close button is rendered');
  assert.equal(app.doc.body.style.overflow, 'hidden', 'page scroll locked');
  assert.equal(background.getAttribute('aria-hidden'), 'true', 'background is hidden from assistive tech');
  assert.equal(background.hasAttribute('inert'), true, 'background is removed from interaction order');
  assert.equal(background.inert, true, 'inert property is enabled as well as the attribute');
  assert.equal(app.doc.activeElement, close, 'focus moves to the explicit close control');
  assert.equal(close.focusOptions && close.focusOptions.preventScroll, true);

  trigger.focus();
  const tabAllowed = overlay.dispatchEvent({ type: 'keydown', key: 'Tab' });
  assert.equal(tabAllowed, false, 'Tab is trapped by preventing its default navigation');
  assert.equal(app.doc.activeElement, close, 'Tab returns focus to the close control');
  trigger.focus();
  const shiftTabAllowed = overlay.dispatchEvent({ type: 'keydown', key: 'Tab', shiftKey: true });
  assert.equal(shiftTabAllowed, false, 'Shift+Tab is trapped too');
  assert.equal(app.doc.activeElement, close, 'Shift+Tab returns focus to the close control');

  // The white pan/zoom surface itself, not just the X, must clear the iPhone
  // sensor housing. Otherwise the top of the rendered card sits under the
  // Dynamic Island even though the close control is reachable.
  const surface = overlay.querySelector('div');
  assert.match(surface.className, /bg-white/, 'the safe-area gutter stays white');
  assert.match(surface.className, /md:items-center/, 'desktop surface vertically centers a fitted map');
  assert.match(surface.className, /md:justify-center/, 'desktop surface horizontally centers a fitted map');
  assert.match(
    surface.getAttribute('style'),
    /padding-top:calc\(env\(safe-area-inset-top, 0px\) \+ 12px\)/,
    'map content starts below the iPhone safe area plus a small visual gutter',
  );

  // The phone remains fit-to-width, while a desktop must fit the initial map
  // inside both viewport axes instead of stretching a tall PNG to 100vw.
  const img = overlay.querySelector('img');
  assert.ok(img, 'the map image is rendered inside the overlay');
  assert.match(img.className, /w-full/, 'mobile map remains fit-to-width');
  assert.match(img.className, /md:w-auto/, 'desktop width follows the fitted aspect ratio');
  assert.match(img.className, /md:max-w-\[calc\(100vw-48px\)\]/, 'desktop width is viewport-bounded');
  assert.match(img.className, /md:max-h-\[calc\(100vh-48px\)\]/, 'desktop height is viewport-bounded');

  // Taps belong to pan/zoom. Neither the image nor the backdrop may dismiss it.
  img.dispatchEvent({ type: 'click' });
  assert.ok(app.doc.getElementById('mm-overlay'), 'tapping the map must not close it');

  overlay.querySelector('div').dispatchEvent({ type: 'click' });
  assert.ok(app.doc.getElementById('mm-overlay'), 'tapping the backdrop must not close it');

  overlay.dispatchEvent({ type: 'click' });
  assert.ok(app.doc.getElementById('mm-overlay'), 'tapping the overlay must not close it');

  // only the explicit X button does
  close.dispatchEvent({ type: 'click' });
  assert.equal(app.doc.getElementById('mm-overlay'), null, 'X closes it');
  assert.equal(app.doc.body.style.overflow, 'scroll', 'the exact prior scroll state is restored');
  assert.equal(background.getAttribute('aria-hidden'), 'legacy', 'prior aria-hidden value is restored exactly');
  assert.equal(background.getAttribute('inert'), 'legacy', 'prior inert attribute value is restored exactly');
  assert.equal(background.inert, true, 'prior inert property is restored exactly');
  assert.equal(app.doc.activeElement, trigger, 'focus returns to the opening control');
  assert.equal(trigger.focusOptions && trigger.focusOptions.preventScroll, true);
});



test('mobile header keeps refresh separate and makes PLAUD polling a compact accessible download control', async () => {
  const rowsRef = { rows: [feedRow()] };
  const calls = [];
  const app = boot((url, options) => {
    calls.push({ url, options });
    if (url === '/api/csrf') return { csrf: 'test-csrf' };
    if (url === '/api/source/poll') return { accepted: true };
    return feedHandler(rowsRef)(url);
  });
  await app.flush();

  const refresh = app.doc.querySelector('[data-view-refresh]');
  const source = app.doc.querySelector('[data-source-poll]');
  const status = app.doc.querySelector('[data-source-poll-status]');
  assert.ok(refresh, 'refresh stays present');
  assert.ok(source, 'source poll stays separate from refresh');
  assert.ok(status, 'exactly one compact live status remains available');
  assert.equal(app.doc.querySelectorAll('[data-source-poll]').length, 1);
  assert.equal(app.doc.querySelectorAll('[data-source-poll-status]').length, 1);
  assert.equal(source.getAttribute('aria-label'), 'Проверить PLAUD');
  assert.equal(source.getAttribute('title'), 'Проверить PLAUD');
  assert.match(source.className, /min-w-11/, 'source target is at least 44px wide');
  assert.match(source.className, /min-h-11/, 'source target is at least 44px tall');
  assert.match(source.innerHTML, /<svg[\s\S]*<path d="M12 3v12/, 'source icon is a download arrow');
  assert.match(source.innerHTML, /<path d="M5 15v4/, 'source icon includes a download tray');
  assert.equal(status.getAttribute('aria-live'), 'polite');
  assert.doesNotMatch(app.html(), /w-full[^>]*data-source-poll|data-source-poll[^>]*w-full/, 'poll is no longer a full-width feed action');
  assert.doesNotMatch(app.html(), /<section[^>]*>[\s\S]*data-source-poll/, 'poll is not rendered in a feed section');

  const header = app.doc.querySelector('header');
  assert.match(header.innerHTML, /data-view-refresh[\s\S]*data-source-poll[\s\S]*aria-label="Архив"[\s\S]*aria-label="Поиск"/,
    '320px header uses compact controls plus discoverable archive and search');
  assert.doesNotMatch(header.innerHTML, />Проверить PLAUD<\//, 'source control remains icon-only visually');
  const archive = app.doc.querySelector('[data-archive-nav]');
  assert.ok(archive); assert.equal(archive.textContent.trim(), ''); assert.ok(archive.getAttribute('aria-label'));

  source.dispatchEvent({ type: 'click' });
  source.dispatchEvent({ type: 'click' });
  await app.flush();
  assert.equal(calls.filter(call => call.url === '/api/source/poll').length, 1, 'duplicate taps post once while disabled');
  assert.equal(calls.find(call => call.url === '/api/source/poll').options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(source.disabled, true, 'source remains disabled until its bounded delay finishes');
  assert.equal(status.innerHTML, 'Проверка PLAUD запущена');

  const before = app.requests.filter(url => url.startsWith('/api/recordings?')).length;
  await app.evalIn('refreshCurrentView(document.querySelector("[data-view-refresh]"))');
  await app.flush();
  assert.equal(app.requests.filter(url => url.startsWith('/api/recordings?')).length, before + 1);
  assert.equal(app.doc.querySelector('[data-source-poll-status]').textContent, 'Проверка PLAUD запущена',
    'the live feedback survives the delayed feed re-render');
  assert.doesNotMatch(fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'), /location\.reload/);
});

test('PLAUD poll reports the server-provided Russian error without duplicating the header status', async () => {
  const app = boot((url) => {
    if (url === '/api/csrf') return { csrf: 'test-csrf' };
    if (url === '/api/source/poll') return {
      __status: 503,
      __body: { error_ru: 'PLAUD временно недоступен.' },
    };
    if (url.startsWith('/api/recordings?')) return [feedRow()];
    throw new Error('unexpected request ' + url);
  });
  await app.flush();

  app.doc.querySelector('[data-source-poll]').dispatchEvent({ type: 'click' });
  await app.flush();
  const statuses = app.doc.querySelectorAll('[data-source-poll-status]');
  assert.equal(statuses.length, 1);
  assert.equal(statuses[0].textContent, 'PLAUD временно недоступен.');
});

test('owner public share posts explicit toggles, copies the fragment URL, and revokes it', async () => {
  const shareId = 'share_opaque_123456789';
  const sharedUrl = `https://share.example.com/${shareId}#ps1_private_fragment`;
  const app = boot((url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/api/csrf') return { csrf: 'test-csrf' };
    if (url === '/api/recordings/rec1' && method === 'GET') {
      return detailPayload({
        public_share_enabled: true,
        public_shares: [],
        summary: '# Итог',
        transcript: 'готово',
        has_mindmap: true,
        has_summary_card: true,
      });
    }
    if (url === '/api/recordings/rec1/public-shares' && method === 'POST') {
      return { share_id: shareId, url: sharedUrl, expires_at: '2026-08-22T13:00:00Z' };
    }
    if (url === `/api/public-shares/${shareId}` && method === 'DELETE') {
      return { revoked: true };
    }
    throw new Error(`unexpected ${method} ${url}`);
  }, { hash: '#/r/rec1' });
  await app.flush();

  for (const name of ['metadata', 'summary', 'images', 'audio']) {
    const field = app.doc.getElementById('public-share-' + name);
    field.checked = true;
    field.disabled = false;
  }
  for (const name of ['transcript', 'mindmap']) {
    const field = app.doc.getElementById('public-share-' + name);
    field.checked = false;
    field.disabled = false;
  }
  app.doc.getElementById('public-share-ttl').value = '7d';
  app.doc.querySelector('[data-public-share]').dispatchEvent({ type: 'click' });
  await app.flush();

  const post = app.fetchCalls.find((call) => call.options.method === 'POST');
  assert.equal(post.url, '/api/recordings/rec1/public-shares');
  assert.deepEqual(JSON.parse(post.options.body), {
    ttl: '7d',
    content: {
      metadata: true,
      summary: true,
      transcript: false,
      mindmap: false,
      images: true,
      audio: true,
    },
  });
  assert.deepEqual(app.clipboardWrites, [sharedUrl]);
  assert.ok(app.fetchCalls.every((call) => !call.url.includes('ps1_private_fragment')));

  app.doc.querySelector('[data-public-revoke]').dispatchEvent({ type: 'click' });
  await app.flush();
  const del = app.fetchCalls.find((call) => call.options.method === 'DELETE');
  assert.equal(del.url, `/api/public-shares/${shareId}`);
  assert.match(app.doc.getElementById('public-share-status').textContent, /Ссылка отозвана/);
});

test('public-share clipboard failure revokes the freshly exported share', async () => {
  const shareId = 'public_share_cleanup_123';
  const app = boot((url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/api/csrf') return { csrf: 'test-csrf' };
    if (url === '/api/recordings/rec1' && method === 'GET') {
      return detailPayload({ public_share_enabled: true, public_shares: [], transcript: 'готово' });
    }
    if (url === '/api/recordings/rec1/public-shares' && method === 'POST') {
      return { share_id: shareId, url: 'https://share.example.com/example#secret', expires_at: 'later' };
    }
    if (url === `/api/public-shares/${shareId}` && method === 'DELETE') return { revoked: true };
    throw new Error(`unexpected ${method} ${url}`);
  }, {
    hash: '#/r/rec1',
    clipboardWrite: async () => { throw new Error('denied'); },
  });
  await app.flush();

  app.doc.querySelector('[data-public-share]').dispatchEvent({ type: 'click' });
  await app.flush();

  const cleanup = app.fetchCalls.find((call) => call.options.method === 'DELETE');
  assert.equal(cleanup.url, `/api/public-shares/${shareId}`);
  assert.match(app.doc.getElementById('public-share-status').textContent, /Экспорт удалён/);
  assert.equal(app.doc.querySelector('[data-public-revoke]'), null);
});


test('home uses 20-row cursor pages and appends the next page without duplicates', async () => {
  const calls = [];
  const app = boot((url) => {
    calls.push(url);
    if (url === '/api/recordings?page=cursor') return { items: Array.from({length: 20}, (_, i) => feedRow({id: 'r' + i, name: 'R' + i})), next_cursor: 'next-20' };
    if (url === '/api/recordings?page=cursor&cursor=next-20') return { items: Array.from({length: 5}, (_, i) => feedRow({id: 'r' + (20+i), name: 'R' + (20+i)})), next_cursor: null };
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  assert.equal(app.doc.querySelectorAll('[data-rec-id]').length, 20);
  const more = app.doc.querySelector('[data-load-more]');
  assert.ok(more);
  assert.match(more.textContent, /Показать ещё/);
  app.tap(more); await app.flush();
  assert.equal(app.doc.querySelectorAll('[data-rec-id]').length, 25);
  assert.equal(app.doc.querySelector('[data-load-more]'), null);
  assert.deepEqual(calls.slice(0, 2), ['/api/recordings?page=cursor', '/api/recordings?page=cursor&cursor=next-20']);
});

test('one persisted global RU EN switch localizes the app and requests English summaries', async () => {
  const requests = [];
  const app = boot((url) => {
    requests.push(url);
    if (url === '/api/recordings?page=cursor') return {items: [feedRow()], next_cursor: null};
    if (url === '/api/recordings?page=cursor&lang=en') return {items: [feedRow({title: 'Сегодня Записи', summary: 'English summary'})], next_cursor: null};
    if (url === '/api/recordings/rec1?lang=en') return detailPayload({summary: 'English summary', summary_card_data:{overview:'English result',themes:[{title:'Topic',detail:'Detail'}],facts:[{value:'Fact'}],decisions:['Decision'],risks:['Risk'],tasks:[{id:'t1',text:'Send it',completed:false}]}, asr_route:{selected_engine:'large-v3',route_reason:'forced'}, public_share_enabled:true, transcript: 'Архив Записи Сегодня', asr_transcript: 'Архив Записи Сегодня'});
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  const toggle = app.doc.querySelector('[data-language-toggle]');
  assert.ok(toggle, 'single global switch exists');
  assert.match(app.html(), /Записи/);
  app.tap(toggle); await app.flush();
  assert.equal(app.evalIn("localStorage.getItem('audio-ui-language')"), 'en');
  assert.match(app.html(), /Recordings/);
  assert.match(app.html(), /Archive/);
  assert.match(app.html(), /August 8/);
  assert.doesNotMatch(app.html(), /августа/);
  assert.match(app.html(), /Сегодня Записи/);
  await app.navigate('#/r/rec1');
  assert.ok(requests.includes('/api/recordings/rec1?lang=en'));
  assert.match(app.html(), /Summary/);
  app.evalIn("showTab('transcript')");
  assert.match(app.html(), /Архив Записи Сегодня/);
  app.evalIn("showTab('summary')");
  for (const russianChrome of ['КРАТКОЕ РЕЗЮМЕ','ГЛАВНЫЕ ТЕМЫ','КЛЮЧЕВЫЕ ФАКТЫ','ЗАДАЧИ','Движок','Причина выбора','Название и дата','Создать и скопировать']) assert.doesNotMatch(app.html(), new RegExp(russianChrome));
  assert.equal((app.html().match(/data-language-toggle/g) || []).length, 1);
});


test('English summary shows honest pending and failed retry states', async () => {
  const state = { detail: detailPayload({summary: null, summary_data: null, summary_language: 'en', summary_state: {language:'en', state:'queued', retryable:false}}) };
  const posts = [];
  const app = boot((url, options = {}) => {
    if (url === '/api/recordings?page=cursor') return {items:[feedRow()],next_cursor:null};
    if (url === '/api/recordings?page=cursor&lang=en') return {items:[feedRow()],next_cursor:null};
    if (url === '/api/recordings/rec1?lang=en') return state.detail;
    if (url === '/api/csrf') return {csrf:'token'};
    if (url === '/api/recordings/rec1/summary/en/retry') { posts.push(options); return {state:'queued'}; }
    throw new Error('unexpected request ' + url);
  });
  await app.flush(); app.tap(app.doc.querySelector('[data-language-toggle]')); await app.flush(); await app.navigate('#/r/rec1');
  assert.match(app.html(), /English summary is queued/);
  state.detail = detailPayload({summary: null, summary_data: null, summary_language:'en', summary_state:{language:'en',state:'failed',retryable:true}});
  await app.navigate('#/r/rec1');
  const retry = app.doc.querySelector('[data-summary-retry]'); assert.ok(retry); assert.match(retry.textContent,/Retry/);
  app.tap(retry); await app.flush();
  assert.equal(posts.length,1); assert.equal(posts[0].method,'POST'); assert.equal(posts[0].headers['X-CSRF-Token'],'token');
});


test('reader accepts an arbitrary report language independently of the shell', async () => {
  const app = boot((url) => {
    if (url === '/api/recordings?page=cursor') return {items:[feedRow()],next_cursor:null};
    if (url === '/api/recordings?page=cursor&lang=ja') return {items:[feedRow({summary:'日本語'})],next_cursor:null};
    if (url === '/api/recordings/rec1') return detailPayload();
    if (url === '/api/recordings/rec1?lang=ja') return detailPayload({summary:'日本語',summary_language:'ja',summary_state:{language:'ja',state:'ready',retryable:false}});
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');
  app.evalIn("setSummaryLanguage('ja')");
  await app.flush();
  assert.ok(app.requests.includes('/api/recordings/rec1?lang=ja'));
  assert.match(app.html(), /data-summary-language/);
  assert.match(app.html(), /value="ja"/);
});


test('archive uses the same stable 20-row cursor paging', async () => {
  const calls=[];
  const first=Array.from({length:20},(_,i)=>feedRow({id:`a${i}`,title:`Archived ${i}`}));
  const second=[feedRow({id:'a20',title:'Archived 20'})];
  const app=boot((url)=>{calls.push(url); if(url==='/api/recordings?page=cursor') return {items:[feedRow()],next_cursor:null}; if(url==='/api/recordings?archived=1&page=cursor') return {items:first,next_cursor:'arc'}; if(url==='/api/recordings?archived=1&page=cursor&cursor=arc') return {items:second,next_cursor:null}; throw new Error('unexpected '+url);});
  await app.flush(); await app.navigate('#/archive');
  assert.equal(calls.filter(x=>x==='/api/recordings?archived=1&page=cursor').length,1);
  assert.equal(app.doc.querySelectorAll('[data-rec-id]').length,20);
  const more=app.doc.querySelector('[data-archive-more]'); assert.ok(more); app.tap(more); await app.flush();
  assert.equal(app.doc.querySelectorAll('[data-rec-id]').length,21); assert.equal(app.doc.querySelector('[data-archive-more]'),null);
});
