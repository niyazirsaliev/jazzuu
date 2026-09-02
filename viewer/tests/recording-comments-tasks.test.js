'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./helpers/minidom.js');
const STATIC = path.join(__dirname, '..', 'app', 'static');

function detailPayload(over = {}) {
  const payload = Object.assign({
    id: 'rec1', name: 'Планёрка', recording_number: 'N-0042',
    start_at: '2026-08-08 10:00:00', duration_ms: 14000, lang: 'ru',
    engine: 'host', segments: [], asr_transcript: 'текст',
    plaud_transcript: null, transcript: 'текст', summary: 'Итог',
    summary_data: { overview: 'Кратко' }, asr_route: {}, asr_processing: null,
    has_summary_card: false, has_audio: false, has_mindmap: false,
    comments: [], tasks: [], labels: [],
    label_definitions: [
      { id: 'personal', name: 'Личное', kind: 'system' },
      { id: 'business', name: 'Работа', kind: 'system' },
    ],
  }, over);
  if (!Object.prototype.hasOwnProperty.call(over, 'summary_card_data')) {
    payload.summary_card_data = {
      overview: payload.summary_data && payload.summary_data.overview || '',
      themes: [], facts: [], decisions: [], risks: [], tasks: payload.tasks,
    };
  }
  return payload;
}

function boot(handler) {
  const doc = createDocument('<html><body><div id="app"></div></body></html>');
  const listeners = new Map();
  const requests = [];
  const sandbox = {
    document: doc, location: { hash: '#/r/rec1' }, console,
    setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => { throw new Error('no intervals'); },
    fetch: (url, options = {}) => {
      requests.push({ url, options });
      return Promise.resolve(handler(url, options)).then((payload) => ({
        ok: !(payload && payload.__status), status: (payload && payload.__status) || 200,
        json: () => Promise.resolve((payload && payload.body) || payload),
      }));
    },
  };
  sandbox.window = sandbox; sandbox.self = sandbox; sandbox.scrollTo = () => {};
  sandbox.addEventListener = (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(fn);
  };
  sandbox.navigator = { serviceWorker: undefined };
  const ctx = vm.createContext(sandbox);
  for (const name of ['asr-progress.js', 'app.js']) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, name), 'utf8'), ctx, { filename: name });
  }
  return {
    doc, ctx, requests,
    flush: async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); },
    html: () => doc.getElementById('app').innerHTML,
    evalIn: (expr) => vm.runInContext(expr, ctx),
  };
}

test('detail renders escaped persistent comments below the tab body with a bounded textarea', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings/rec1')) return detailPayload({
      comments: [{ id: 'c1', text: '<b>Важно</b>', created_at: '2026-08-09T10:00:00Z' }],
    });
    throw new Error('unexpected request ' + url);
  });
  await app.flush();

  const comments = app.doc.querySelector('[data-comments]');
  assert.ok(comments, 'comments block is rendered');
  assert.match(comments.innerHTML, /&lt;b&gt;Важно&lt;\/b&gt;/);
  assert.match(comments.textContent, /9 августа.*\d{2}:\d{2}/);
  assert.equal(comments.querySelector('textarea').getAttribute('maxlength'), '2000');
  assert.ok(comments.querySelector('[data-comment-add]'), 'add control is rendered');
  assert.ok(app.html().indexOf('id="detail-body"') < app.html().indexOf('data-comments'),
    'comments follow the tab body rather than belonging to a tab');
});

test('detail summary renders only the canonical card payload', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings/rec1')) return detailPayload({
      summary_data: { overview: 'Старое расходящееся резюме' },
      summary_card_data: {
        overview: 'Канонический итог', themes: [], facts: [],
        decisions: [], risks: [], tasks: [],
      },
    });
    throw new Error('unexpected request ' + url);
  });
  await app.flush();

  assert.match(app.html(), /Канонический итог/);
  assert.doesNotMatch(app.html(), /Старое расходящееся резюме/);
});

test('adding a comment posts its text with CSRF and appends the returned comment', async () => {
  const app = boot((url, options) => {
    if (url === '/api/csrf') return { csrf: 'csrf-token' };
    if (url === '/api/recordings/rec1/comments' && options.method === 'POST') {
      return { comment: { id: 'c2', text: 'Новый текст', created_at: '2026-08-09T10:00:00Z' } };
    }
    if (url.startsWith('/api/recordings/rec1')) return detailPayload();
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  app.doc.querySelector('[data-comment-text]').value = '  Новый текст  ';
  app.doc.querySelector('[data-comment-add]').dispatchEvent({ type: 'click' });
  await app.flush();

  const post = app.requests.find((request) => request.options.method === 'POST');
  assert.equal(post.url, '/api/recordings/rec1/comments');
  assert.equal(post.options.headers['X-CSRF-Token'], 'csrf-token');
  assert.deepEqual(JSON.parse(post.options.body), { text: 'Новый текст' });
  assert.match(app.doc.querySelector('[data-comments-list]').textContent, /Новый текст/);
  assert.equal(app.doc.querySelector('[data-comment-text]').value, '', 'saved text is cleared');
});

test('generated tasks are accessible controls that toggle and persist completion', async () => {
  const app = boot((url, options) => {
    if (url === '/api/csrf') return { csrf: 'csrf-token' };
    if (url === '/api/recordings/rec1/tasks/t1' && options.method === 'POST') {
      return { task: { id: 't1', completed: true } };
    }
    if (url.startsWith('/api/recordings/rec1')) return detailPayload({
      tasks: [{ id: 't1', text: 'Подготовить отчёт', owner: 'Аня', due: 'Завтра', completed: false }],
    });
    throw new Error('unexpected request ' + url);
  });
  await app.flush();

  const task = app.doc.querySelector('[data-task-id]');
  assert.ok(task, 'generated task control is rendered');
  assert.equal(task.localName, 'button');
  assert.equal(task.getAttribute('role'), 'checkbox');
  assert.equal(task.getAttribute('aria-checked'), 'false');
  assert.match(task.textContent, /Подготовить отчёт/);
  task.dispatchEvent({ type: 'click' });
  await app.flush();

  const post = app.requests.find((request) => request.options.method === 'POST');
  assert.equal(post.url, '/api/recordings/rec1/tasks/t1');
  assert.deepEqual(JSON.parse(post.options.body), { completed: true });
  const done = app.doc.querySelector('[data-task-id]');
  assert.equal(done.getAttribute('aria-checked'), 'true');
  assert.match(done.textContent, /✓/);
  assert.match(done.className, /line-through/);
});

test('a second task tap reopens it and failed comments keep the typed text', async () => {
  const app = boot((url, options) => {
    if (url === '/api/csrf') return { csrf: 'csrf-token' };
    if (url === '/api/recordings/rec1/tasks/t1' && options.method === 'POST') {
      const completed = JSON.parse(options.body).completed;
      return { task: { id: 't1', completed } };
    }
    if (url === '/api/recordings/rec1/comments' && options.method === 'POST') {
      return { __status: 503, body: { error_ru: 'Сервис временно недоступен.' } };
    }
    if (url.startsWith('/api/recordings/rec1')) return detailPayload({
      tasks: [{ id: 't1', text: 'Подготовить отчёт', completed: false }],
    });
    throw new Error('unexpected request ' + url);
  });
  await app.flush();

  app.doc.querySelector('[data-task-id]').dispatchEvent({ type: 'click' });
  await app.flush();
  app.doc.querySelector('[data-task-id]').dispatchEvent({ type: 'click' });
  await app.flush();
  const taskPosts = app.requests.filter((request) => request.url.includes('/tasks/'));
  assert.deepEqual(taskPosts.map((request) => JSON.parse(request.options.body)),
    [{ completed: true }, { completed: false }]);
  assert.equal(app.doc.querySelector('[data-task-id]').getAttribute('aria-checked'), 'false');

  const input = app.doc.querySelector('[data-comment-text]');
  input.value = 'Не потерять этот текст';
  app.doc.querySelector('[data-comment-add]').dispatchEvent({ type: 'click' });
  await app.flush();
  assert.equal(app.doc.querySelector('[data-comment-text]').value, 'Не потерять этот текст');
  assert.match(app.doc.querySelector('[data-comments-status]').textContent, /временно недоступен/);
});

test('detail omits labels and assignment mutations while retaining backend payload compatibility', async () => {
  const app = boot((url) => {
    if (url.startsWith('/api/recordings/rec1')) return detailPayload({
      labels: ['personal'],
      label_definitions: [
        { id: 'personal', name: 'Личное', kind: 'system' },
        { id: 'x', name: '<img src=x>', kind: 'custom' },
      ],
    });
    throw new Error('unexpected request ' + url);
  });
  await app.flush();
  assert.equal(app.doc.querySelector('[data-recording-labels]'), null);
  assert.equal(app.doc.querySelector('[data-label-id]'), null);
  assert.doesNotMatch(app.html(), /Личное|&lt;img src=x&gt;/);
  assert.equal(app.requests.some((request) => request.url.endsWith('/labels')), false);
});
