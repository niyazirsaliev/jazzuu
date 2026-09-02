// Per-tenant display time zone in the PWA.
//
// Like app-integration.test.js this evaluates the REAL app/static sources in a
// VM against the minidom stand-in, and drives them through the same fetch entry
// points the browser uses. Nothing here re-implements app logic.
//
// The bug this pins: the app formatted timestamps with Date#getHours(), which
// is the *reader's* zone. On this server (UTC) a recording made at 14:16 in
// New York rendered as 18:16 for everyone. The fix is an explicit per-tenant
// IANA zone from the API, applied via Intl -- so these tests deliberately run
// the VM with a process zone that matches nobody, and assert the rendered text.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./helpers/minidom.js');

const STATIC = path.join(__dirname, '..', 'app', 'static');
const SOURCES = ['asr-progress.js', 'app.js'];   // exactly index.html's order

function boot(handler, opts = {}) {
  const doc = createDocument('<html><body><div id="app"></div></body></html>');
  const timers = new Map();
  let seq = 0;
  const location = { hash: opts.hash || '#/' };
  const winListeners = new Map();
  const storage = new Map(Object.entries(opts.storage || {}));

  // "Today"/"Yesterday" headings are relative to the wall clock, so a test
  // asserting on a date heading has to pin it. Only the zero-argument form is
  // pinned; every other Date behaviour is the real one.
  const RealDate = Date;
  function PinnedDate(...args) {
    if (!(this instanceof PinnedDate)) return RealDate(...args);
    if (args.length === 0 && opts.now) return new RealDate(opts.now);
    return new RealDate(...args);
  }
  PinnedDate.prototype = RealDate.prototype;
  PinnedDate.now = () => (opts.now ? new RealDate(opts.now).getTime() : RealDate.now());
  PinnedDate.UTC = RealDate.UTC;
  PinnedDate.parse = RealDate.parse;

  const sandbox = {
    document: doc,
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
    },
    location,
    console,
    Intl,
    Date: PinnedDate,
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    setInterval: () => { throw new Error('app must not use setInterval'); },
    fetch: (url, options) => Promise.resolve(handler(url, options)).then((payload) => {
      if (payload && payload.__status) {
        return { ok: false, status: payload.__status, json: () => Promise.resolve(payload.__body || null) };
      }
      return { ok: true, status: 200, json: () => Promise.resolve(payload) };
    }),
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.scrollTo = () => {};
  sandbox.addEventListener = (type, fn) => {
    if (!winListeners.has(type)) winListeners.set(type, []);
    winListeners.get(type).push(fn);
  };
  sandbox.navigator = { serviceWorker: undefined, clipboard: { writeText: async () => {} } };

  const ctx = vm.createContext(sandbox);
  for (const name of SOURCES) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, name), 'utf8'), ctx, { filename: name });
  }
  return {
    doc,
    html: () => doc.getElementById('app').innerHTML,
    text: () => doc.getElementById('app').innerHTML.replace(/<[^>]*>/g, ' '),
    evalIn: (expr) => vm.runInContext(expr, ctx),
    flush: async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); },
    async navigate(hash) {
      location.hash = hash;
      for (const fn of winListeners.get('hashchange') || []) fn();
      await this.flush();
    },
  };
}

function feedRow(over = {}) {
  return Object.assign({
    id: 'rec1',
    name: 'Запись',
    title: 'Запись',
    start_at: '2026-08-20T18:16:10',
    start_at_utc: '2026-08-20T18:16:10+00:00',
    duration_ms: 60000,
    lang: 'ru',
    summary: null,
    asr_processing: null,
  }, over);
}

function feedHandler(payload) {
  return (url) => {
    if (url.startsWith('/api/recordings?')) return payload;
    throw new Error('unexpected request ' + url);
  };
}

// The feed helper takes the same options object boot() does.
function bootFeed(payload, opts) {
  return boot(feedHandler(payload), opts);
}

test('the feed renders the recording time in the tenant zone, not the browser zone', async () => {
  // 18:16:10 UTC is 14:16 in New York. The VM's own zone is UTC here, so a
  // browser-zone implementation would print 18:16 and fail this assertion.
  const app = boot(feedHandler({
    items: [feedRow()],
    next_cursor: null,
    display_timezone: 'America/New_York',
  }));
  await app.flush();

  assert.match(app.text(), /14:16/);
  assert.doesNotMatch(app.text(), /18:16/);
});

test('a UTC instant after midnight shows the previous calendar day in New York', async () => {
  // 2026-08-21T02:30Z is still 22:30 on 2026-08-20 in America/New_York. A
  // day-rollover bug puts this recording under the wrong date heading.
  const app = bootFeed({
    items: [feedRow({
      start_at: '2026-08-21T02:30:00',
      start_at_utc: '2026-08-21T02:30:00+00:00',
    })],
    next_cursor: null,
    display_timezone: 'America/New_York',
  }, { now: '2026-08-25T12:00:00Z' });
  await app.flush();

  assert.match(app.text(), /22:30/);
  assert.match(app.text(), /20 августа/);
  assert.doesNotMatch(app.text(), /21 августа/);
});

test('a UTC instant before midnight shows the next calendar day in Dhaka', async () => {
  // 2026-08-20T20:10Z is 02:10 on 2026-08-21 in Asia/Dhaka (UTC+6).
  const app = bootFeed({
    items: [feedRow({
      start_at: '2026-08-20T20:10:00',
      start_at_utc: '2026-08-20T20:10:00+00:00',
    })],
    next_cursor: null,
    display_timezone: 'Asia/Dhaka',
  }, { now: '2026-08-25T12:00:00Z' });
  await app.flush();

  assert.match(app.text(), /02:10/);
  assert.match(app.text(), /21 августа/);
  assert.doesNotMatch(app.text(), /20 августа/);
});

test('New York times straddling a DST transition keep their true local hour', async () => {
  // US DST ends 2026-11-01 at 06:00Z. 05:30Z is 01:30 EDT (UTC-4) and 06:30Z
  // is 01:30 EST (UTC-5) — a fixed offset gets exactly one of these wrong.
  const app = boot(feedHandler({
    items: [
      feedRow({ id: 'edt', title: 'До перехода',
                start_at: '2026-11-01T05:30:00',
                start_at_utc: '2026-11-01T05:30:00+00:00' }),
      feedRow({ id: 'est', title: 'После перехода',
                start_at: '2026-11-01T06:30:00',
                start_at_utc: '2026-11-01T06:30:00+00:00' }),
    ],
    next_cursor: null,
    display_timezone: 'America/New_York',
  }));
  await app.flush();

  const text = app.text();
  assert.match(text, /01:30/);   // the EDT recording
  assert.match(text, /01:30/);   // and the EST one, same wall clock
  assert.doesNotMatch(text, /02:30/);
  assert.doesNotMatch(text, /00:30/);
});

test('Dhaka has no DST, so a January and a July instant use the same offset', async () => {
  const app = boot(feedHandler({
    items: [
      feedRow({ id: 'jan', start_at: '2026-01-15T04:00:00',
                start_at_utc: '2026-01-15T04:00:00+00:00' }),
      feedRow({ id: 'jul', start_at: '2026-07-15T04:00:00',
                start_at_utc: '2026-07-15T04:00:00+00:00' }),
    ],
    next_cursor: null,
    display_timezone: 'Asia/Dhaka',
  }));
  await app.flush();

  const times = [...app.text().matchAll(/\b(\d{2}:\d{2})\b/g)].map(m => m[1]);
  assert.deepEqual(times.filter(t => t === '10:00').length, 2);
});

test('the detail metadata line uses the tenant zone', async () => {
  const detail = {
    id: 'rec1', name: 'Запись', title: 'Запись',
    start_at: '2026-08-20T18:16:10',
    start_at_utc: '2026-08-20T18:16:10+00:00',
    display_timezone: 'America/New_York',
    duration_ms: 60000, lang: 'ru', engine: 'host',
    segments: [], transcript: '', summary: null, summary_data: null,
    asr_route: {}, asr_processing: null, comments: [], tasks: [],
    has_summary_card: false, has_audio: false, has_mindmap: false,
  };
  const app = boot((url) => {
    if (url.startsWith('/api/recordings/rec1')) return detail;
    if (url.startsWith('/api/recordings?')) return { items: [], next_cursor: null, display_timezone: 'America/New_York' };
    throw new Error('unexpected request ' + url);
  }, { hash: '#/r/rec1' });
  await app.flush();

  assert.match(app.text(), /20\.08\.26 · 14:16/);
});

test('an unconfigured tenant keeps the previous browser-zone behaviour', async () => {
  const app = bootFeed({
    items: [feedRow()],
    next_cursor: null,
    display_timezone: null,
  });
  await app.flush();

  // Whatever zone this runner happens to be in, an unconfigured tenant must
  // still render that zone's own wall clock -- unchanged from before the fix.
  const local = new Date('2026-08-20T18:16:10+00:00');
  const expected = `${String(local.getHours()).padStart(2, '0')}:`
    + `${String(local.getMinutes()).padStart(2, '0')}`;
  assert.match(app.text(), new RegExp(expected));
});

test('comment timestamps render in the tenant zone', async () => {
  const detail = {
    id: 'rec1', name: 'Запись', title: 'Запись',
    start_at: '2026-08-20T18:16:10',
    start_at_utc: '2026-08-20T18:16:10+00:00',
    display_timezone: 'America/New_York',
    duration_ms: 60000, lang: 'ru', engine: 'host',
    segments: [], transcript: '', summary: null, summary_data: null,
    asr_route: {}, asr_processing: null, tasks: [],
    comments: [{ id: 1, text: 'заметка',
                 created_at: '2026-08-21T02:40:00',
                 created_at_utc: '2026-08-21T02:40:00+00:00' }],
    has_summary_card: false, has_audio: false, has_mindmap: false,
  };
  const app = boot((url) => {
    if (url.startsWith('/api/recordings/rec1')) return detail;
    if (url.startsWith('/api/recordings?')) return { items: [], next_cursor: null, display_timezone: 'America/New_York' };
    throw new Error('unexpected request ' + url);
  }, { hash: '#/r/rec1' });
  await app.flush();

  // 02:40Z on the 21st is 22:40 on the 20th in New York.
  assert.match(app.text(), /20 августа, 22:40/);
});

test('the archive list adopts the tenant zone from its own response', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings?archived=1')) {
      return { items: [feedRow()], next_cursor: null, display_timezone: 'Asia/Dhaka' };
    }
    if (url.startsWith('/api/recordings?')) return { items: [], next_cursor: null, display_timezone: 'Asia/Dhaka' };
    throw new Error('unexpected request ' + url);
  }, { hash: '#/archive' });
  await app.flush();

  assert.equal(app.evalIn('displayTimezone'), 'Asia/Dhaka');
});
