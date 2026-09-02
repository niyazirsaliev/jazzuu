// Pure renderer for long-ASR progress, shared by the feed card and the detail
// view. Kept in its own dependency-free file for two reasons: app.js touches
// the DOM at load time and cannot be imported by a test runner, and this is the
// only piece of the UI worth pinning down with deterministic tests.
//
// Input is the `asr_processing` object the API derives from asr_segments — or
// null, which means "render nothing". Everything is string-in / string-out:
// no DOM, no timers, no clock. There is deliberately no elapsed or remaining
// time anywhere; nothing in the data supports one.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const STATE_COLOR = {
    processing: '#7c6cf0',
    error: '#e0715f',
    pending: '#8e8e98',
  };
  const FALLBACK_LABEL = {
    processing: 'Расшифровывается',
    error: 'Ошибка блока · повторится автоматически',
    pending: 'Ожидает расшифровки',
  };

  function escAttr(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  // Whole 0..100, however odd the payload. The server already floors it; this
  // only stops a malformed value from producing a NaN-width bar.
  function asrPercent(p) {
    const n = Math.floor(Number(p && p.percent));
    if (!isFinite(n)) return 0;
    return Math.max(0, Math.min(100, n));
  }

  function asrProgressHtml(p) {
    if (!p || typeof p !== 'object') return '';
    const state = STATE_COLOR[p.state] ? p.state : 'pending';
    if (!(Number(p.total) > 0)) return '';
    const pct = asrPercent(p);
    const label = String(p.label_ru || FALLBACK_LABEL[state]);
    const color = STATE_COLOR[state];
    return (
      // No outer margin: the caller's slot owns spacing, so an empty slot
      // (`empty:hidden`) leaves no gap on cards with nothing to report.
      `<div class="asr-progress" data-asr-state="${escAttr(state)}">` +
        '<div class="flex items-center gap-2 min-w-0">' +
          `<span class="text-[11.5px] font-semibold leading-tight rounded-full px-2 py-0.5 truncate" ` +
            `style="color:${color};background:${color}1f">${escAttr(label)}</span>` +
          `<span class="text-[11.5px] text-mut tabular-nums shrink-0">${pct}%</span>` +
        '</div>' +
        '<div class="mt-1.5 h-1.5 rounded-full bg-white/10 overflow-hidden" ' +
          `role="progressbar" aria-valuemin="0" aria-valuemax="100" ` +
          `aria-valuenow="${pct}" aria-valuetext="${pct}%" ` +
          `aria-label="${escAttr(label)}">` +
          `<div class="h-full rounded-full transition-[width] duration-500" ` +
            `style="width:${pct}%;background:${color}"></div>` +
        '</div>' +
      '</div>'
    );
  }

  // Polling predicate: keep refreshing only while something is actually in
  // flight. Accepts the feed array or a single detail payload.
  function anyAsrActive(items) {
    if (!items || typeof items !== 'object') return false;
    const list = Array.isArray(items) ? items : [items];
    return list.some((r) => r && r.asr_processing);
  }

  // id -> html its `[data-asr-id]` slot should hold. An empty string means the
  // recording finished and the slot must be cleared.
  function asrSlotUpdates(items) {
    const out = new Map();
    if (!items || typeof items !== 'object') return out;
    const list = Array.isArray(items) ? items : [items];
    for (const r of list) {
      if (!r || r.id == null) continue;
      out.set(String(r.id), asrProgressHtml(r.asr_processing));
    }
    return out;
  }

  // Everything the feed *cards* render apart from progress, in one comparable
  // string. Equal keys mean "same list, same cards": a silent refresh may then
  // patch the badges and leave the DOM (and the reader's scroll) alone.
  // Different keys mean a recording appeared, vanished, was renamed or got a
  // summary — the feed has to be rebuilt.
  function feedCardsKey(rows) {
    if (!Array.isArray(rows)) return null;
    return JSON.stringify(rows.map((r) => [
      (r && r.id) || '',
      (r && r.name) || '',
      (r && r.start_at) || '',
      r && r.duration_ms != null ? r.duration_ms : null,
      (r && r.lang) || '',
      (r && r.summary) || '',
    ]));
  }

  // Everything #detail-body renders, in one comparable string. Values, not
  // truthiness: a summary rewritten from one non-empty text to another, a
  // corrected transcript, or a re-diarised segment list that happens to keep
  // its length all have to count as changed, or the reader keeps staring at
  // superseded text.
  //
  // Deliberately excluded: `asr_processing`'s numbers (the badge is patched in
  // place, and rebuilding the body on every tick would fight the reader), and
  // anything rendered *outside* the body — the title and the audio player,
  // which must survive a refresh untouched. Only whether progress exists at
  // all is included, because that flips the transcript tab's placeholder.
  function detailContentKey(r) {
    if (!r || typeof r !== 'object') return null;
    const segments = Array.isArray(r.segments) ? r.segments : [];
    return JSON.stringify([
      r.asr_transcript || '',
      r.plaud_transcript || '',
      r.summary || '',
      r.summary_data == null ? null : r.summary_data,
      r.asr_route == null ? null : r.asr_route,
      r.engine || '',
      Boolean(r.has_summary_card),
      Boolean(r.has_mindmap),
      Boolean(r.asr_processing),
      segments.map((s) => [
        (s && s.speaker) || '',
        s && s.start_ms != null ? s.start_ms : null,
        (s && s.text) || '',
        // The name shown above a turn is part of the turn as rendered: a rename
        // changes the transcript on screen without touching a single segment.
        (s && s.display_name) || '',
      ]),
      // The Спикеры tab. Values, not truthiness: a name the reader saved on
      // another device, a diarization stage that moved from pending to
      // unavailable, a re-derived snippet interval and a job status line are all
      // things the body draws, so all of them have to count as changed.
      r.speaker_names == null ? null : r.speaker_names,
      speakerModelKey(r.speakers),
      r.jobs == null ? null : r.jobs,
    ]);
  }

  // The part of the speaker model the body actually renders. Spelled out rather
  // than serialised whole so a field added to the API for some other reader does
  // not start rebuilding the body — and the reader's scroll position — on every
  // fifteen-second tick.
  function speakerModelKey(model) {
    if (!model || typeof model !== 'object') return null;
    const list = Array.isArray(model.speakers) ? model.speakers : [];
    return [
      model.state || '',
      model.note_ru || '',
      Boolean(model.truncated),
      model.can_edit === false ? false : true,
      list.map((s) => [
        (s && s.speaker_id) || '',
        (s && s.source_label) || '',
        (s && s.display_name) || '',
        (s && s.name_ru) || '',
        s && s.segment_count != null ? s.segment_count : null,
        s && s.total_ms != null ? s.total_ms : null,
        s && s.snippet ? [s.snippet.start_ms, s.snippet.end_ms] : null,
      ]),
    ];
  }

  // True when a refreshed detail payload changed something the body renders.
  // A ticking percentage does not qualify: patching the badge in place keeps
  // the reader's scroll position, and the audio element (which lives outside
  // the body) is never touched.
  function detailNeedsRerender(before, after) {
    if (!after) return false;
    if (!before) return true;
    return detailContentKey(before) !== detailContentKey(after);
  }

  // The one refresh loop. Invariants it exists to guarantee:
  //   * at most one armed timer, ever — re-notifying replaces, never stacks
  //   * armed only while something is genuinely transcribing
  //   * never armed while the document is hidden
  //   * one refresh in flight at a time
  //   * a transient failure retries; an auth wall ends the loop
  // Timers and visibility are injected so this is testable on a fake clock.
  function createAsrPoller(opts) {
    const o = opts || {};
    const intervalMs = o.intervalMs || 15000;
    const fetchData = o.fetchData;
    const isHidden = o.isHidden || (() => false);
    const setTimer = o.setTimer || ((fn, ms) => setTimeout(fn, ms));
    const clearTimer = o.clearTimer || ((id) => clearTimeout(id));

    let timer = null;
    let active = false;
    let busy = false;

    function disarm() {
      if (timer !== null) { clearTimer(timer); timer = null; }
    }

    function arm() {
      disarm();
      if (!active || busy || isHidden()) return;
      timer = setTimer(tick, intervalMs);
    }

    async function tick() {
      timer = null;
      if (!active || busy || isHidden()) return arm();
      busy = true;
      try {
        const data = await fetchData();
        // null/undefined = the view moved on mid-flight; keep what we knew.
        if (data != null) active = anyAsrActive(data);
      } catch (e) {
        if (e && e.message === 'unauth') active = false;
        // anything else is transient: keep the loop alive and try again
      } finally {
        busy = false;
      }
      arm();
    }

    return {
      // Feed the freshest payload in after any render or refresh.
      note(data) { active = anyAsrActive(data); arm(); },
      stop() { active = false; disarm(); },
      onVisibilityChange() {
        if (isHidden()) { disarm(); return Promise.resolve(); }
        if (!active || busy) { arm(); return Promise.resolve(); }
        disarm();
        return tick();   // returning to the app shows fresh numbers at once
      },
      isActive() { return active; },
      isArmed() { return timer !== null; },
    };
  }

  return {
    asrProgressHtml,
    anyAsrActive,
    asrPercent,
    asrSlotUpdates,
    feedCardsKey,
    detailContentKey,
    detailNeedsRerender,
    createAsrPoller,
  };
});
