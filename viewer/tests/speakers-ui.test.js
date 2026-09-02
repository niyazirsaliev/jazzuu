// Integration test for the Спикеры tab and the reprocess controls, driving the
// REAL app/static/asr-progress.js + app/static/app.js in a VM against minidom.
//
// Same rule as app-integration.test.js: nothing here re-implements app logic.
// fetch, timers, visibility, location.hash and the two <audio> methods jsdom-less
// minidom does not have (play/pause) are stubbed — every one of them a browser
// API, not a piece of the app.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./helpers/minidom.js');

const STATIC = path.join(__dirname, '..', 'app', 'static');
const SOURCES = ['asr-progress.js', 'identity-selector.js', 'app.js'];

const SPEAKERS = {
  state: 'ready',
  source: 'plaud_segments',
  reason: null,
  count: 2,
  truncated: false,
  note_ru: null,
  speakers: [
    {
      speaker_id: 'speaker-1', source_label: 'Speaker 1', display_name: 'Аня',
      name_ru: 'Аня', segment_count: 2, total_ms: 9000,
      snippet: { start_ms: 4000, end_ms: 9000 },
    },
    {
      speaker_id: 'speaker-2', source_label: 'Speaker 2', display_name: null,
      name_ru: 'Спикер 2', segment_count: 1, total_ms: 5000,
      snippet: null,
    },
  ],
};

function detailPayload(over = {}) {
  return Object.assign({
    id: 'rec1',
    name: 'Планёрка',
    start_at: '2026-08-08 10:00:00',
    duration_ms: 14000,
    lang: 'ru',
    engine: 'host',
    segments: [
      { speaker: 'Speaker 1', speaker_id: 'speaker-1', display_name: 'Аня',
        start_ms: 0, end_ms: 6000, text: 'Привет' },
      { speaker: 'Speaker 2', speaker_id: 'speaker-2', display_name: null,
        start_ms: 6000, end_ms: 11000, text: 'Как дела' },
    ],
    asr_transcript: '[Speaker 1] Привет',
    plaud_transcript: null,
    transcript: '[Speaker 1] Привет',
    summary: 'Старое резюме',
    summary_data: { overview: 'Старый итог' },
    asr_route: {},
    asr_processing: null,
    has_summary_card: true,
    has_audio: true,
    has_mindmap: false,
    speakers: SPEAKERS,
    speaker_names: { 'speaker-1': 'Аня' },
    jobs: {},
  }, over);
}

// Boot the production shell. `handler(url, options)` answers every request.
function boot(handler, opts = {}) {
  const doc = createDocument('<html><body><div id="app"></div></body></html>');
  const timers = new Map();
  let seq = 0;
  const requests = [];
  const location = { hash: opts.hash || '#/' };
  const winListeners = new Map();

  const sandbox = {
    document: doc,
    location,
    console,
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    setInterval: () => { throw new Error('app must not use setInterval'); },
    fetch: (url, options) => {
      requests.push({ url, options: options || {} });
      return Promise.resolve(handler(url, options || {})).then((payload) => {
        if (payload && payload.__status) {
          return {
            ok: payload.__status < 400, status: payload.__status,
            json: () => Promise.resolve(payload.body || null),
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
  sandbox.navigator = { serviceWorker: undefined };

  const ctx = vm.createContext(sandbox);
  for (const name of SOURCES) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, name), 'utf8'), ctx,
      { filename: name });
  }

  const api = {
    doc,
    requests,
    location,
    urls: () => requests.map((r) => r.url),
    posts: () => requests.filter((r) => (r.options.method || 'GET') !== 'GET'),
    html: () => doc.getElementById('app').innerHTML,
    evalIn: (expr) => vm.runInContext(expr, ctx),
    flush: async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); },
    async navigate(hash) {
      location.hash = hash;
      for (const fn of winListeners.get('hashchange') || []) fn();
      await api.flush();
    },
    // The player the app already renders, with the two methods minidom lacks.
    audio() {
      const el = doc.getElementById('rec-audio');
      if (el && !el.play) {
        el.readyState = 1;
        el.currentTime = 0;
        el.paused = true;
        el.playCount = 0;
        el.pauseCount = 0;
        el.play = () => { el.playCount++; el.paused = false; return Promise.resolve(); };
        el.pause = () => { el.pauseCount++; el.paused = true; };
      }
      return el;
    },
    async click(selector) {
      const el = doc.querySelector(selector);
      assert.ok(el, `no element matched ${selector}`);
      el.dispatchEvent({ type: 'click' });
      await api.flush();
      return el;
    },
  };
  return api;
}

function handlerFor(state) {
  return (url, options = {}) => {
    if (url.startsWith('/api/csrf')) return { csrf: 'csrf-token-value' };
    if (url === '/api/people' && (options.method || 'GET') === 'GET') return {
      people: [{ person_id: 'p_aaaaaaaaaaaaaaaaaaaaaaaa', display_name: 'Анна',
        is_self: false, consent_status: 'not_enrolled', active: true, revision: 1 }],
    };
    if (url.includes('/identity/undo')) return { assignment: { person_id: null } };
    if (url.includes('/identity')) return { assignment: { person_id: 'p_aaaaaaaaaaaaaaaaaaaaaaaa', display_name: 'Анна' } };
    if (url.startsWith('/api/recordings?')) return [];
    if (url.startsWith('/api/recordings/rec1/speakers')) return state.speakers || SPEAKERS;
    if (url.startsWith('/api/recordings/rec1/reprocess')) return state.reprocess;
    if (url.startsWith('/api/recordings/rec1')) return state.detail;
    throw new Error('unexpected request ' + url);
  };
}

async function openSpeakers(state) {
  const app = boot(handlerFor(state));
  await app.flush();
  await app.navigate('#/r/rec1');
  app.evalIn("showTab('speakers')");
  await app.flush();
  return app;
}

// ---------------- the tab itself ----------------

test('the detail view offers a third Спикеры tab', async () => {
  const app = boot(handlerFor({ detail: detailPayload() }));
  await app.flush();
  await app.navigate('#/r/rec1');

  assert.match(app.html(), /Резюме/);
  assert.match(app.html(), /Транскрипт/);
  assert.match(app.html(), /Спикеры/);
  assert.ok(app.doc.getElementById('tab-speakers'), 'the tab control exists');
});

test('the tab shows the truthful count, raw labels and editable names', async () => {
  const app = await openSpeakers({ detail: detailPayload() });

  const html = app.html();
  assert.match(html, /2 спикера/, 'the count is the number of real labels');
  assert.match(html, /Speaker 1/, 'the raw source label stays visible');
  assert.match(html, /Speaker 2/);

  const rows = app.doc.querySelectorAll('[data-speaker-row]');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].getAttribute('data-speaker-id'), 'speaker-1');

  const inputs = app.doc.querySelectorAll('[data-speaker-name]');
  assert.equal(inputs.length, 2);
  assert.equal(inputs[0].getAttribute('value'), 'Аня', 'the saved name is prefilled');
  assert.equal(inputs[1].getAttribute('value'), '');
  assert.match(inputs[1].getAttribute('placeholder') || '', /Спикер 2|Имя/);
  // Accessible: every field names itself, and the fields are real inputs.
  for (const input of inputs) {
    assert.equal(input.localName, 'input');
    assert.ok(input.getAttribute('aria-label'), 'name field has an accessible label');
    assert.equal(input.getAttribute('maxlength'), '60', 'the server bound is mirrored');
  }
});

test('manual identity selector offers known, new, unknown and undo without voice enrollment', async () => {
  const app = await openSpeakers({ detail: detailPayload() });
  const selects = app.doc.querySelectorAll('[data-identity-person]');
  assert.equal(selects.length, 2);
  assert.match(app.html(), /Известный человек/);
  assert.match(app.html(), /Новый человек/);
  assert.match(app.html(), /Неизвестный/);
  assert.match(app.html(), /Отменить/);
  assert.doesNotMatch(app.html(), /embedding|обуч/i);
  selects[0].value = 'p_aaaaaaaaaaaaaaaaaaaaaaaa';
  selects[0].dispatchEvent({ type: 'change' });
  await app.flush();
  const posted = app.posts().find((r) => r.url.includes('/identity'));
  assert.ok(posted);
  assert.deepEqual(JSON.parse(posted.options.body), { person_id: 'p_aaaaaaaaaaaaaaaaaaaaaaaa' });
  assert.equal(posted.options.headers['X-CSRF-Token'], 'csrf-token-value');
});

test('an accepted voiceprint is shown as a readable identity badge without embeddings', async () => {
  const speakers = JSON.parse(JSON.stringify(SPEAKERS));
  speakers.speakers[0].voiceprint = {
    identity: 'Айбек', confidence: 0.909767, margin: 0.909767,
  };
  const app = await openSpeakers({ detail: detailPayload({ speakers }), speakers });

  assert.match(app.html(), /Распознано: Айбек/);
  assert.match(app.html(), /91%/);
  assert.doesNotMatch(app.html(), /embedding/i);
});

test('a recording with no diarization says so instead of showing a count', async () => {
  const app = await openSpeakers({
    detail: detailPayload({
      speakers: {
        state: 'unavailable', source: null, reason: 'no_labels', count: 0,
        truncated: false, speakers: [],
        note_ru: 'Разметка спикеров недоступна: в записи нет сегментов с метками спикеров.',
      },
      speaker_names: {},
    }),
  });

  const html = app.html();
  assert.match(html, /Разметка спикеров недоступна/);
  assert.equal(app.doc.querySelectorAll('[data-speaker-row]').length, 0);
  assert.equal(app.doc.querySelectorAll('[data-speaker-name]').length, 0);
  assert.ok(!/0 спикеров/.test(html) || /недоступна/.test(html));
  assert.ok(!/1 спикер\b/.test(html), 'never a fabricated speaker');
});

test('a queued diarization stage reads as pending, not as zero speakers', async () => {
  const app = await openSpeakers({
    detail: detailPayload({
      speakers: {
        state: 'pending', source: null, reason: 'pending', count: 0,
        truncated: false, speakers: [],
        note_ru: 'Разметка спикеров в очереди на обработку.',
      },
    }),
  });

  assert.match(app.html(), /в очереди на обработку/);
});

test('names and labels are escaped, never injected as markup', async () => {
  const app = await openSpeakers({
    detail: detailPayload({
      speakers: Object.assign({}, SPEAKERS, {
        count: 1,
        speakers: [{
          speaker_id: 'speaker-1',
          source_label: '<img src=x onerror=alert(1)>',
          display_name: '"><script>alert(2)</script>',
          name_ru: '"><script>alert(2)</script>',
          voiceprint: { identity: '<img src=x onerror=alert(3)>', confidence: 0.9 },
          segment_count: 1, total_ms: 1000,
          snippet: { start_ms: 0, end_ms: 1000 },
        }],
      }),
    }),
  });

  const html = app.html();
  assert.ok(!html.includes('<script>'), 'no raw script tag reached the DOM');
  assert.ok(!html.includes('<img src=x'), 'no raw img tag reached the DOM');
  assert.match(html, /&lt;img src=x/);
  assert.equal(app.doc.querySelectorAll('script').length, 0);
});

// ---------------- bounded snippet playback ----------------

test('play uses the existing private player, bounded to the snippet', async () => {
  const app = await openSpeakers({ detail: detailPayload() });
  const audio = app.audio();
  assert.ok(audio, 'the detail view already renders the private player');
  assert.equal(audio.getAttribute('src'), '/audio/rec1',
    'playback goes through the authenticated Range endpoint');

  const button = app.doc.querySelector('[data-snippet-play]');
  assert.ok(button, 'the speaker with a snippet has a play control');
  assert.equal(button.getAttribute('data-start-ms'), '4000');
  assert.equal(button.getAttribute('data-end-ms'), '9000');
  assert.ok(button.getAttribute('aria-label'), 'the play control names itself');

  const before = app.urls().length;
  button.dispatchEvent({ type: 'click' });
  await app.flush();

  assert.equal(audio.currentTime, 4, 'seeks to the snippet start');
  assert.equal(audio.playCount, 1, 'plays the existing element');
  assert.equal(app.urls().length, before, 'no clip is created or fetched');
  assert.equal(app.doc.querySelectorAll('audio').length, 1,
    'no second audio element, so nothing is downloaded twice');

  // Bounded: reaching the end of the snippet stops playback by itself.
  audio.currentTime = 8.9;
  audio.dispatchEvent({ type: 'timeupdate' });
  assert.equal(audio.pauseCount, 0, 'still inside the snippet');
  audio.currentTime = 9.05;
  audio.dispatchEvent({ type: 'timeupdate' });
  assert.equal(audio.pauseCount, 1, 'stops at the snippet end');
});

test('a speaker without a usable snippet gets no play button', async () => {
  const app = await openSpeakers({ detail: detailPayload() });

  const rows = app.doc.querySelectorAll('[data-speaker-row]');
  assert.ok(rows[0].querySelector('[data-snippet-play]'));
  assert.equal(rows[1].querySelector('[data-snippet-play]'), null);
  assert.match(rows[1].innerHTML, /Фрагмент недоступен|нет фрагмента/i);
});

// ---------------- saving names ----------------

test('saving posts only names, with the CSRF header, and reports in Russian', async () => {
  const state = { detail: detailPayload() };
  const app = await openSpeakers(state);

  const input = app.doc.querySelectorAll('[data-speaker-name]')[1];
  input.value = '  Борис  ';
  state.speakers = Object.assign({}, SPEAKERS, {
    speakers: [SPEAKERS.speakers[0],
      Object.assign({}, SPEAKERS.speakers[1], { display_name: 'Борис',
        name_ru: 'Борис' })],
  });
  await app.click('[data-speakers-save]');

  const posts = app.posts();
  assert.equal(posts.length, 1, 'exactly one mutation');
  const post = posts[0];
  assert.equal(post.url, '/api/recordings/rec1/speakers');
  assert.equal(post.options.method, 'POST');
  assert.equal(post.options.credentials, 'same-origin');
  assert.equal(post.options.headers['X-CSRF-Token'], 'csrf-token-value');
  const body = JSON.parse(post.options.body);
  assert.deepEqual(Object.keys(body), ['names']);
  assert.equal(body.names['speaker-2'], 'Борис', 'trimmed on the way out');
  assert.ok(!('speaker-1' in body.names) || body.names['speaker-1'] === 'Аня');
  assert.ok(!JSON.stringify(body).includes('Привет'), 'no transcript is sent back');

  assert.match(app.doc.querySelector('[data-speakers-status]').innerHTML,
    /Имена сохранены|Сохранено/);
});

test('the status region announces itself to assistive tech', async () => {
  const app = await openSpeakers({ detail: detailPayload() });
  const status = app.doc.querySelector('[data-speakers-status]');

  assert.ok(status, 'there is a status region');
  assert.equal(status.getAttribute('aria-live'), 'polite');
  assert.equal(status.getAttribute('role'), 'status');
});

test('a rejected save shows the safe Russian message and keeps the field', async () => {
  const state = { detail: detailPayload() };
  const app = boot((url, options) => {
    if (url.startsWith('/api/csrf')) return { csrf: 'csrf-token-value' };
    if (url.startsWith('/api/recordings?')) return [];
    if (url.startsWith('/api/recordings/rec1/speakers') && options.method === 'POST') {
      return { __status: 400, body: { error_ru: 'Не удалось обработать запрос.' } };
    }
    if (url.startsWith('/api/recordings/rec1/speakers')) return SPEAKERS;
    if (url.startsWith('/api/recordings/rec1')) return state.detail;
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');
  app.evalIn("showTab('speakers')");
  await app.flush();

  const input = app.doc.querySelectorAll('[data-speaker-name]')[1];
  input.value = 'Борис';
  await app.click('[data-speakers-save]');

  const status = app.doc.querySelector('[data-speakers-status]').innerHTML;
  assert.match(status, /Не удалось/);
  assert.ok(!status.includes('Traceback'));
  assert.equal(app.doc.querySelectorAll('[data-speaker-name]')[1].value, 'Борис',
    'the reader does not lose what they typed');
});

// ---------------- reprocess controls ----------------

test('both reprocess actions are offered in Russian and post the right action',
  async () => {
    const state = { detail: detailPayload(),
      reprocess: { action: 'transcript', state: 'queued',
        message_ru: 'Запрос принят: перетранскрибирование поставлено в очередь.',
        jobs: { retranscribe: { state: 'queued', message_ru: 'В очереди на обработку.' } } } };
    const app = await openSpeakers(state);

    assert.match(app.html(), /Перетранскрибировать/);
    assert.match(app.html(), /Пересобрать материалы/);

    await app.click('[data-reprocess="transcript"]');
    let post = app.posts()[0];
    assert.equal(post.url, '/api/recordings/rec1/reprocess');
    assert.equal(post.options.method, 'POST');
    assert.equal(post.options.headers['X-CSRF-Token'], 'csrf-token-value');
    assert.deepEqual(JSON.parse(post.options.body), { action: 'transcript' });
    assert.match(app.doc.querySelector('[data-reprocess-status]').innerHTML,
      /перетранскрибирование поставлено в очередь/);

    state.reprocess = { action: 'materials', state: 'queued',
      message_ru: 'Запрос принят: материалы будут пересобраны.', jobs: {} };
    await app.click('[data-reprocess="materials"]');
    post = app.posts()[1];
    assert.deepEqual(JSON.parse(post.options.body), { action: 'materials' });
  });

test('a double tap sends one request and the button locks itself', async () => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const state = { detail: detailPayload() };
  const app = boot((url, options) => {
    if (url.startsWith('/api/csrf')) return { csrf: 'csrf-token-value' };
    if (url.startsWith('/api/recordings?')) return [];
    if (url.startsWith('/api/recordings/rec1/reprocess')) return held;
    if (url.startsWith('/api/recordings/rec1/speakers')) return SPEAKERS;
    if (url.startsWith('/api/recordings/rec1')) return state.detail;
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  await app.navigate('#/r/rec1');
  app.evalIn("showTab('speakers')");
  await app.flush();

  const button = app.doc.querySelector('[data-reprocess="transcript"]');
  button.dispatchEvent({ type: 'click' });
  await app.flush();
  button.dispatchEvent({ type: 'click' });   // impatient second tap
  await app.flush();

  assert.equal(app.posts().length, 1, 'the in-flight request is not duplicated');
  assert.equal(button.hasAttribute('disabled'), true, 'the control locks itself');

  release({ action: 'transcript', state: 'queued', message_ru: 'В очереди.',
    jobs: { retranscribe: { state: 'queued', message_ru: 'В очереди на обработку.' } } });
  await app.flush();
  assert.match(app.doc.querySelector('[data-reprocess-status]').innerHTML, /В очереди/);
});

test('a queued job keeps the current material on screen and shows its state',
  async () => {
    const app = await openSpeakers({
      detail: detailPayload({
        jobs: {
          retranscribe: { state: 'queued', message_ru: 'В очереди на обработку.' },
        },
      }),
    });

    // The Спикеры tab reports the job…
    assert.match(app.html(), /В очереди на обработку/);
    // …and the reader's existing summary and transcript are still right there.
    app.evalIn("showTab('summary')");
    assert.match(app.html(), /Старое резюме/);
    app.evalIn("showTab('transcript')");
    assert.match(app.html(), /Привет/);
  });

test('the transcript tab shows assigned names and keeps the raw label available',
  async () => {
    const app = boot(handlerFor({ detail: detailPayload() }));
    await app.flush();
    await app.navigate('#/r/rec1');
    app.evalIn("showTab('transcript')");
    await app.flush();

    const html = app.html();
    assert.match(html, /Аня/, 'the assigned name is what the reader sees');
    assert.match(html, /Спикер 2/, 'an unnamed speaker keeps its Russian label');
    assert.ok(!/\[Speaker 1\]/.test(html), 'the raw label is not shown as a heading');
  });

test('a poll that brings new names re-renders the open tab', async () => {
  const state = { detail: detailPayload() };
  const app = boot(handlerFor(state));
  await app.flush();
  await app.navigate('#/r/rec1');
  app.evalIn("showTab('speakers')");
  await app.flush();
  assert.match(app.html(), /Аня/);

  state.detail = detailPayload({
    speaker_names: { 'speaker-1': 'Анна Петровна' },
    speakers: Object.assign({}, SPEAKERS, {
      speakers: [Object.assign({}, SPEAKERS.speakers[0],
        { display_name: 'Анна Петровна', name_ru: 'Анна Петровна' }),
        SPEAKERS.speakers[1]],
    }),
  });
  await app.evalIn('refreshDetail("rec1", _navGen)');
  await app.flush();

  assert.match(app.html(), /Анна Петровна/, 'the tab followed the new data');
});
