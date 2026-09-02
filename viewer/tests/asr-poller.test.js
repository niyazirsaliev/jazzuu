// Deterministic tests for the ASR refresh loop.
//
// The poller owns the one rule that is easy to get wrong and impossible to see
// in a screenshot: exactly one 15s timer, only while something is actually
// transcribing, never while the tab is hidden, never two at once. Timers and
// visibility are injected so these tests run on a fake clock — no sleeping, no
// flakiness.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  createAsrPoller,
  detailNeedsRerender,
  asrSlotUpdates,
} = require('../app/static/asr-progress.js');

const ACTIVE = [{ id: 'a', asr_processing: { state: 'processing', total: 4, percent: 25 } }];
const IDLE = [{ id: 'a', asr_processing: null }];

// Minimal fake clock: records pending timers, lets a test fire them by hand.
function harness(opts = {}) {
  let seq = 0;
  const timers = new Map();
  const fetches = [];
  let hidden = false;
  const queue = opts.responses ? opts.responses.slice() : [];

  const poller = createAsrPoller({
    intervalMs: 15000,
    isHidden: () => hidden,
    setTimer: (fn, ms) => {
      const id = ++seq;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    fetchData: () => {
      fetches.push(1);
      const next = queue.length ? queue.shift() : ACTIVE;
      return typeof next === 'function' ? next() : Promise.resolve(next);
    },
  });

  return {
    poller,
    timers,
    fetchCount: () => fetches.length,
    pending: () => [...timers.values()],
    hide: () => { hidden = true; },
    show: () => { hidden = false; },
    // fire the single pending timer
    async fire() {
      assert.equal(timers.size, 1, 'expected exactly one pending timer');
      const [id, t] = [...timers.entries()][0];
      timers.delete(id);
      await t.fn();
    },
  };
}

test('an active payload arms exactly one 15s timer', () => {
  const h = harness();
  h.poller.note(ACTIVE);
  assert.equal(h.timers.size, 1);
  assert.equal(h.pending()[0].ms, 15000);
});

test('an idle payload arms nothing', () => {
  const h = harness();
  h.poller.note(IDLE);
  assert.equal(h.timers.size, 0);
  assert.equal(h.poller.isActive(), false);
});

test('repeated notes never leave a second timer behind', () => {
  const h = harness();
  for (let i = 0; i < 5; i++) h.poller.note(ACTIVE);
  assert.equal(h.timers.size, 1);
});

test('keeps polling while work is in flight', async () => {
  const h = harness({ responses: [ACTIVE, ACTIVE] });
  h.poller.note(ACTIVE);
  await h.fire();
  assert.equal(h.fetchCount(), 1);
  assert.equal(h.timers.size, 1);
  await h.fire();
  assert.equal(h.fetchCount(), 2);
  assert.equal(h.timers.size, 1);
});

test('stops as soon as nothing is transcribing any more', async () => {
  const h = harness({ responses: [IDLE] });
  h.poller.note(ACTIVE);
  await h.fire();
  assert.equal(h.fetchCount(), 1);
  assert.equal(h.timers.size, 0, 'must not re-arm once everything is done');
  assert.equal(h.poller.isActive(), false);
});

test('a hidden document is never polled', async () => {
  const h = harness();
  h.hide();
  h.poller.note(ACTIVE);
  assert.equal(h.timers.size, 0);
  assert.equal(h.fetchCount(), 0);
});

test('going hidden disarms, coming back catches up once', async () => {
  const h = harness();
  h.poller.note(ACTIVE);
  assert.equal(h.timers.size, 1);
  h.hide();
  h.poller.onVisibilityChange();
  assert.equal(h.timers.size, 0);
  h.show();
  await h.poller.onVisibilityChange();
  assert.equal(h.fetchCount(), 1, 'should refresh immediately on return');
  assert.equal(h.timers.size, 1);
});

test('visibility changes while idle stay idle', async () => {
  const h = harness();
  h.poller.note(IDLE);
  h.show();
  await h.poller.onVisibilityChange();
  assert.equal(h.fetchCount(), 0);
  assert.equal(h.timers.size, 0);
});

test('a failed refresh retries instead of giving up', async () => {
  const h = harness({ responses: [() => Promise.reject(new Error('http 502'))] });
  h.poller.note(ACTIVE);
  await h.fire();
  assert.equal(h.timers.size, 1, 'transient failure must not stop the loop');
  assert.equal(h.poller.isActive(), true);
});

test('an auth wall stops the loop for good', async () => {
  const h = harness({ responses: [() => Promise.reject(new Error('unauth'))] });
  h.poller.note(ACTIVE);
  await h.fire();
  assert.equal(h.timers.size, 0);
  assert.equal(h.poller.isActive(), false);
});

test('a null payload (view changed mid-flight) keeps the previous state', async () => {
  const h = harness({ responses: [null] });
  h.poller.note(ACTIVE);
  await h.fire();
  assert.equal(h.timers.size, 1);
});

test('stop() disarms everything', () => {
  const h = harness();
  h.poller.note(ACTIVE);
  h.poller.stop();
  assert.equal(h.timers.size, 0);
  assert.equal(h.poller.isActive(), false);
});

test('never runs two refreshes at once', async () => {
  let release;
  const gate = new Promise((res) => { release = res; });
  const h = harness({ responses: [() => gate.then(() => ACTIVE), ACTIVE] });
  h.poller.note(ACTIVE);
  const inFlight = h.fire();
  assert.equal(h.fetchCount(), 1);
  h.show();
  await h.poller.onVisibilityChange();   // would double-fetch without a guard
  assert.equal(h.fetchCount(), 1);
  release();
  await inFlight;
  assert.equal(h.timers.size, 1);
});

test('detailNeedsRerender only rebuilds the body when the content changed', () => {
  const withProgress = { asr_processing: { state: 'processing', total: 2 }, segments: [] };
  const same = { asr_processing: { state: 'processing', total: 2, percent: 50 }, segments: [] };
  const done = { asr_transcript: 'готовый текст', asr_processing: null, segments: [] };

  // progress ticking along: patch the badge, leave the body (and audio) alone
  assert.equal(detailNeedsRerender(withProgress, same), false);
  // transcript arrived: the body must be rebuilt so it replaces the progress
  assert.equal(detailNeedsRerender(withProgress, done), true);
  // progress vanished without a transcript
  assert.equal(detailNeedsRerender(withProgress, { segments: [] }), true);
  // speaker segments landed
  assert.equal(
    detailNeedsRerender(withProgress, { asr_processing: null, segments: [{ text: 'x' }] }),
    true);
  // summary appeared
  assert.equal(detailNeedsRerender(withProgress, { ...same, summary: 'итог' }), true);
  assert.equal(detailNeedsRerender(null, same), true);
  assert.equal(detailNeedsRerender(same, null), false);
});

test('detailNeedsRerender notices speaker names, the speaker model and job state',
  () => {
    // The body renders the Спикеры tab now, so what that tab shows has to be
    // part of "did the content change?". Without this, a name saved on another
    // device — or a job that finished — leaves the open tab showing stale text
    // until the reader navigates away and back.
    const base = {
      segments: [{ speaker: 'Speaker 1', speaker_id: 'speaker-1', text: 'привет',
                   display_name: 'Аня' }],
      speaker_names: { 'speaker-1': 'Аня' },
      speakers: {
        state: 'ready', count: 1,
        speakers: [{ speaker_id: 'speaker-1', source_label: 'Speaker 1',
                     display_name: 'Аня', name_ru: 'Аня',
                     snippet: { start_ms: 0, end_ms: 5000 } }],
      },
      jobs: {},
    };
    const renamed = {
      ...base,
      segments: [{ ...base.segments[0], display_name: 'Анна Петровна' }],
      speaker_names: { 'speaker-1': 'Анна Петровна' },
      speakers: {
        ...base.speakers,
        speakers: [{ ...base.speakers.speakers[0], display_name: 'Анна Петровна',
                     name_ru: 'Анна Петровна' }],
      },
    };

    assert.equal(detailNeedsRerender(base, { ...base }), false, 'identical payload');
    assert.equal(detailNeedsRerender(base, renamed), true, 'a rename must repaint');
    // A diarization stage that moved from pending to unavailable changes the note.
    assert.equal(
      detailNeedsRerender(base, { ...base, speakers: { ...base.speakers, state: 'pending' } }),
      true);
    // A queued job appearing (or clearing) changes the status line on screen.
    assert.equal(
      detailNeedsRerender(base, {
        ...base,
        jobs: { retranscribe: { state: 'queued', message_ru: 'В очереди на обработку.' } },
      }),
      true);
    // A snippet the server re-derived points the play button somewhere else.
    assert.equal(
      detailNeedsRerender(base, {
        ...base,
        speakers: {
          ...base.speakers,
          speakers: [{ ...base.speakers.speakers[0],
                       snippet: { start_ms: 4000, end_ms: 9000 } }],
        },
      }),
      true);
  });

test('asrSlotUpdates maps every id to the html its slot should hold', () => {
  const updates = asrSlotUpdates([
    { id: 'a', asr_processing: { state: 'processing', total: 4, percent: 25, label_ru: 'Расшифровывается · 1 из 4 блоков' } },
    { id: 'b', asr_processing: null },
    null,
    { asr_processing: null },
  ]);
  assert.deepEqual([...updates.keys()], ['a', 'b']);
  assert.match(updates.get('a'), /Расшифровывается · 1 из 4 блоков/);
  assert.equal(updates.get('b'), '', 'a finished recording clears its slot');
  // a single detail payload works too
  assert.deepEqual([...asrSlotUpdates({ id: 'z', asr_processing: null }).keys()], ['z']);
});
