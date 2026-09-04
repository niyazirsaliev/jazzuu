'use strict';
const app = document.getElementById('app');

const UI_LANGUAGE_KEY = 'audio-ui-language';
let uiLanguage = (() => {
  try { return localStorage.getItem(UI_LANGUAGE_KEY) === 'en' ? 'en' : 'ru'; }
  catch (_) { return 'ru'; }
})();
const SUMMARY_LANGUAGE_KEY = 'audio-summary-language';
function validSummaryLanguage(value) { return /^[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,3}$/.test(String(value || '').trim().toLowerCase()); }
let summaryLanguage = (() => {
  try {
    const saved = localStorage.getItem(SUMMARY_LANGUAGE_KEY);
    return validSummaryLanguage(saved) ? saved.trim().toLowerCase() : uiLanguage;
  } catch (_) { return uiLanguage; }
})();
const EN_TEXT = {
  'Записи': 'Recordings', 'Архив': 'Archive', 'Поиск': 'Search', 'Обновить': 'Refresh',
  'Проверить PLAUD': 'Check PLAUD', 'Показать ещё': 'Load more',
  'Резюме': 'Summary', 'Транскрипт': 'Transcript', 'Спикеры': 'Speakers',
  'Действия': 'Actions', 'Скачать аудио': 'Download audio',
  'Резюме ещё не готово.': 'Summary is not ready yet.',
  'Транскрипт обрабатывается…': 'Transcript is processing…',
  'Пока нет записей': 'No recordings yet',
  'Записи появятся здесь после синхронизации.': 'Recordings will appear here after synchronization.',
  'Не удалось загрузить записи': 'Could not load recordings',
  'Запись не найдена': 'Recording not found', 'Не удалось загрузить архив': 'Could not load archive',
  'Архив пуст': 'Archive is empty', 'Заархивированные записи появятся здесь.': 'Archived recordings will appear here.',
  'Поиск по записям': 'Search recordings', 'Ничего не найдено': 'Nothing found',
  'Попробуйте другой запрос.': 'Try another query.', 'Ошибка поиска': 'Search failed',
  'Сегодня': 'Today', 'Вчера': 'Yesterday', 'Без даты': 'No date',
  'Карта записи': 'Recording map', 'Комментарии': 'Comments',
  'Публичная ссылка': 'Public link', 'Заархивировать запись': 'Archive recording',
  'Обработка': 'Processing', 'Маршрут распознавания': 'Recognition route',
  'Действия с записью': 'Recording actions', 'Скачать аудиозапись': 'Download audio',
  'PLAUD · КРАТКОЕ РЕЗЮМЕ': 'PLAUD · BRIEF SUMMARY', 'КРАТКОЕ РЕЗЮМЕ': 'BRIEF SUMMARY',
  'ГЛАВНЫЕ ТЕМЫ': 'MAIN TOPICS', 'КЛЮЧЕВЫЕ ФАКТЫ': 'KEY FACTS', 'ЗАДАЧИ': 'TASKS',
  'Движок': 'Engine', 'Причина выбора': 'Selection reason', 'Нет данных': 'No data',
  'Название и дата': 'Title and date', 'PNG-карточка': 'PNG card', 'Карта': 'Mind map', 'Аудио': 'Audio',
  '1 час': '1 hour', '24 часа': '24 hours', '7 дней': '7 days', 'Создать и скопировать': 'Create and copy',
  'Ссылка откроет только отмеченные материалы.': 'The link opens only the selected materials.',
  'Перетранскрибировать': 'Retranscribe', 'Пересобрать материалы': 'Rebuild materials',
  'Распознать спикеров': 'Identify speakers', 'Добавить комментарий': 'Add a comment',
  'Новый комментарий': 'New comment', 'Добавить': 'Add', 'Комментариев пока нет.': 'No comments yet.',
  'Разделы записи': 'Recording sections', 'Назад': 'Back', 'Копировать': 'Copy',
  'Удалить запись': 'Delete recording', 'Удалить': 'Delete', 'Отмена': 'Cancel',
  'Восстановить запись': 'Restore recording', 'Запись в архиве': 'Recording is archived',
  'Сохранить имена': 'Save names', 'Сохранение…': 'Saving…', 'Сохранено.': 'Saved.',
  'Разметка спикеров недоступна для этой записи.': 'Speaker labeling is unavailable for this recording.',
  'Разметка спикеров': 'Speaker labeling', 'имена сохраняются только у вас': 'names are saved only for you',
  'Фрагмент недоступен': 'Clip unavailable', 'Прослушать фрагмент': 'Play clip',
  'Распознано': 'Recognized', 'По смыслу': 'Semantic', 'Спикер': 'Speaker',
  'Ключевые факты': 'Key facts', 'Решения': 'Decisions', 'Задачи': 'Action items',
  'Риски': 'Risks', 'Открытые вопросы': 'Open questions', 'Проекты': 'Projects',
  'Главные темы': 'Main themes', 'Краткое резюме': 'Brief summary', 'Тема': 'Theme',
  'Нет данных': 'No data', 'Не выделено.': 'None.', 'Общий вывод': 'Overview',
  'Краткое содержание': 'Brief summary', 'Активные ссылки': 'Active links',
  'Срок действия': 'Expiration', 'Создать и скопировать ссылку': 'Create and copy link',
  'Отозвать': 'Revoke', 'Ссылка отозвана.': 'Link revoked.',
  'Включить аудио': 'Include audio', 'Включить резюме': 'Include summary',
  'Включить карту': 'Include map', 'Включить транскрипт': 'Include transcript',
  'час': 'hour', 'день': 'day', 'дней': 'days',
  'Расшифровывается': 'Transcribing', 'В очереди': 'Queued', 'Ошибка обработки': 'Processing error',
  'Задача выполняется в фоне. Текущий транскрипт, резюме и карта остаются на месте,': 'The job runs in the background. The current transcript, summary, and map remain visible',
  'пока не появится новая готовая версия.': 'until the new version is ready.',
  'расшифровка Barston ASR': 'Barston ASR transcript',
  'Загрузить ещё': 'Load more', 'Поиск…': 'Searching…', ' из ': ' of ',
  ' блоков': ' blocks', ' блока': ' blocks', ' блок': ' block', 'метка': 'label',
};
function tr(value) { return uiLanguage === 'en' ? (EN_TEXT[value] || value) : value; }
function localized(html) {
  if (uiLanguage !== 'en') return html;
  let out = String(html == null ? '' : html);
  for (const [ru, en] of Object.entries(EN_TEXT).sort((a,b) => b[0].length-a[0].length)) out = out.split(ru).join(en);
  return out;
}
function localizedPreserving(html, values) {
  let out = String(html == null ? '' : html);
  const saved = [];
  for (const value of values || []) {
    if (value == null || value === '') continue;
    const rendered = esc(String(value));
    if (!rendered || !out.includes(rendered)) continue;
    const token = `__AUDIO_CONTENT_${saved.length}__`;
    out = out.split(rendered).join(token);
    saved.push([token, rendered]);
  }
  out = localized(out);
  for (const [token, rendered] of saved) out = out.split(token).join(rendered);
  return out;
}
function languageQuery() { return summaryLanguage === 'ru' ? '' : `&lang=${encodeURIComponent(summaryLanguage)}`; }
function languageToggleHtml() {
  return `<button type="button" data-language-toggle aria-label="${uiLanguage === 'en' ? 'Switch interface to Russian' : 'Переключить интерфейс на английский'}" class="min-w-11 min-h-11 px-2 rounded-xl border border-line/60 text-[12px] font-bold">${uiLanguage === 'en' ? 'RU' : 'EN'}</button>`;
}
function toggleLanguage() {
  const followedShell = summaryLanguage === uiLanguage;
  uiLanguage = uiLanguage === 'en' ? 'ru' : 'en';
  try { localStorage.setItem(UI_LANGUAGE_KEY, uiLanguage); } catch (_) {}
  if (followedShell) setSummaryLanguage(uiLanguage, false);
  if (document.documentElement) document.documentElement.setAttribute('lang', uiLanguage);
  _feedCursor = null; _feedRows = [];
  route();
}
function setSummaryLanguage(value, reroute = true) {
  const language = String(value || '').trim().toLowerCase();
  if (!validSummaryLanguage(language)) return false;
  summaryLanguage = language;
  try { localStorage.setItem(SUMMARY_LANGUAGE_KEY, language); } catch (_) {}
  _feedCursor = null; _feedRows = [];
  if (reroute) route();
  return true;
}
function summaryLanguageControlHtml() {
  const label = uiLanguage === 'en' ? 'Report language' : 'Язык отчёта';
  const apply = uiLanguage === 'en' ? 'Apply' : 'Применить';
  return `<div class="mb-3 flex items-end gap-2"><label class="flex-1 text-xs text-mut">${label}<input data-summary-language required pattern="[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}" value="${esc(summaryLanguage)}" placeholder="ru, en, ja, pt-BR" class="mt-1 min-h-11 w-full rounded-xl border border-line/60 bg-ink px-3 text-white"></label><button type="button" data-summary-language-apply class="min-h-11 rounded-xl border border-line px-3 font-semibold">${apply}</button></div>`;
}

// ---------- helpers ----------
function fmtDur(ms) {
  if (!ms || ms < 0) return '';
  const s = Math.round(ms / 1000);
  const m = Math.floor(s / 60);
  const ss = String(s % 60).padStart(2, '0');
  if (m >= 60) {
    const h = Math.floor(m / 60);
    const mm = String(m % 60).padStart(2, '0');
    return `${h}:${mm}:${ss}`;
  }
  return `${m}:${ss}`;
}
// m:ss (or h:mm:ss) from a millisecond offset — used for transcript timestamps
function fmtTs(ms) {
  if (ms == null || isNaN(ms)) return '';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const ss = String(s % 60).padStart(2, '0');
  if (m >= 60) {
    const h = Math.floor(m / 60);
    const mm = String(m % 60).padStart(2, '0');
    return `${h}:${mm}:${ss}`;
  }
  return `${m}:${ss}`;
}
function parseDate(s) {
  if (!s) return null;
  // accept "2026-07-18 22:02:43" or ISO
  let d = new Date(s.replace(' ', 'T'));
  if (isNaN(d)) d = new Date(s);
  return isNaN(d) ? null : d;
}
// A recording's instant, preferring the server's explicit UTC form. The bare
// stored value is naive, and a naive string is parsed by the browser in the
// *reader's* zone — which is exactly the bug this avoids.
function recordingDate(r) {
  return parseDate((r && r.start_at_utc) || (r && r.start_at));
}

// The API publishes the tenant's IANA display zone. `null` means unconfigured,
// so the reader's own zone stays in use.
let displayTimezone = null;
function setDisplayTimezone(value) {
  displayTimezone = (typeof value === 'string' && value) ? value : null;
}
const _zoneFormatters = new Map();
function _zoneFormatter(zone) {
  if (!_zoneFormatters.has(zone)) {
    _zoneFormatters.set(zone, new Intl.DateTimeFormat('en-US', {
      timeZone: zone, year: 'numeric', month: 'numeric', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }));
  }
  return _zoneFormatters.get(zone);
}
// Calendar fields of `d` as seen in the display zone. Every visible date and
// time is derived from these instead of from Date#getHours() and friends.
function zonedParts(d) {
  if (!d || Number.isNaN(d.getTime())) return null;
  if (!displayTimezone) {
    return { year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate(),
             hour: d.getHours(), minute: d.getMinutes() };
  }
  try {
    const parts = {};
    for (const part of _zoneFormatter(displayTimezone).formatToParts(d)) {
      if (part.type !== 'literal') parts[part.type] = Number(part.value);
    }
    if (parts.hour === 24) parts.hour = 0;   // some ICU builds report hour 24
    return { year: parts.year, month: parts.month, day: parts.day,
             hour: parts.hour, minute: parts.minute };
  } catch (_) {
    return { year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate(),
             hour: d.getHours(), minute: d.getMinutes() };
  }
}
const MONTHS = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
function dayKey(d) {
  const p = zonedParts(d);
  return p ? `${p.year}-${p.month}-${p.day}` : 'x';
}
function groupLabel(d) {
  if (!d) return 'Без даты';
  const parts = zonedParts(d);
  if (!parts) return 'Без даты';
  const now = new Date();
  const nowParts = zonedParts(now);
  // "Yesterday" is the day before *today in the display zone*, so step back a
  // day from that calendar date rather than from the reader's clock.
  const yesterdayKey = (() => {
    const anchor = new Date(Date.UTC(nowParts.year, nowParts.month - 1, nowParts.day));
    anchor.setUTCDate(anchor.getUTCDate() - 1);
    return `${anchor.getUTCFullYear()}-${anchor.getUTCMonth() + 1}-${anchor.getUTCDate()}`;
  })();
  const key = `${parts.year}-${parts.month}-${parts.day}`;
  if (key === `${nowParts.year}-${nowParts.month}-${nowParts.day}`) return 'Сегодня';
  if (key === yesterdayKey) return 'Вчера';
  const sameYear = parts.year === nowParts.year;
  if (uiLanguage === 'en') {
    return new Intl.DateTimeFormat('en-US', {
      ...(displayTimezone ? { timeZone: displayTimezone } : {}),
      day: 'numeric', month: 'long', ...(sameYear ? {} : { year: 'numeric' }),
    }).format(d);
  }
  return `${parts.day} ${MONTHS[parts.month - 1]}${sameYear ? '' : ' ' + parts.year}`;
}
function timeLabel(d) {
  const p = zonedParts(d);
  if (!p) return '';
  return `${String(p.hour).padStart(2,'0')}:${String(p.minute).padStart(2,'0')}`;
}
function compactDateTime(d) {
  const p = zonedParts(d);
  if (!p) return '';
  const pad = n => String(n).padStart(2, '0');
  return `${pad(p.day)}.${pad(p.month)}.${String(p.year).slice(-2)} · ${timeLabel(d)}`;
}
function esc(s) {
  return (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
// minimal, safe markdown -> HTML for the summary block (esc first, then format)
function renderMd(src) {
  const lines = (src || '').replace(/\r/g, '').split('\n');
  let html = '', list = null;
  const inline = (t) => esc(t)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code class="bg-white/10 rounded px-1 text-[13px]">$1</code>');
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (let raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { closeList(); continue; }
    let m;
    if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/))) {
      closeList();
      const lvl = m[1].length;
      const cls = lvl <= 2 ? 'text-[15px] font-bold mt-3 mb-1' : 'text-[14px] font-semibold text-mut mt-3 mb-1';
      html += `<div class="${cls}">${inline(m[2])}</div>`;
    } else if ((m = line.match(/^\s{0,3}>\s?(.*)$/))) {
      closeList();
      html += `<div class="border-l-2 border-accent/50 pl-3 my-1.5 text-mut">${inline(m[1])}</div>`;
    } else if ((m = line.match(/^\s{0,3}[-*+]\s+(.*)$/))) {
      if (list !== 'ul') { closeList(); html += '<ul class="list-disc pl-5 my-1.5 space-y-0.5">'; list = 'ul'; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s{0,3}(\d+)\.\s+(.*)$/))) {
      if (list !== 'ol') { closeList(); html += '<ol class="list-decimal pl-5 my-1.5 space-y-0.5">'; list = 'ol'; }
      html += `<li>${inline(m[2])}</li>`;
    } else {
      closeList();
      html += `<p class="my-1.5">${inline(line)}</p>`;
    }
  }
  closeList();
  return html;
}

// Language codes the reader recognises. Kyrgyz shows as KG for display,
// not the ISO 639-1 'ky' the engine emits. A recording with two real languages
// renders as a pair, e.g. RU/KG.
const LANG_LABELS = { ru: 'RU', ky: 'KG', kg: 'KG', en: 'EN', de: 'DE', es: 'ES', kk: 'KK', tr: 'TR' };
function langBadge(lang) {
  if (!lang) return '';
  const parts = String(lang).toLowerCase().split(/[+\/,]/).map(p => p.trim()).filter(Boolean);
  // 'mixed' is a legacy marker with no per-language detail; older rows keep it.
  const l = (parts.length && parts[0] !== 'mixed')
    ? parts.map(p => LANG_LABELS[p] || p.slice(0, 2).toUpperCase()).join('/')
    : 'MIX';
  return `<span class="text-[10px] font-semibold tracking-wide text-accent/90 bg-accent/10 rounded-md px-1.5 py-0.5">${esc(l)}</span>`;
}

// ---------- permanent recording code ----------
// N-0042 is how a person names a recording out loud — to themselves, to the
// family, to a bot. It is shown wherever a recording is. One tap copies a
// contextual MCP reference; the visible code remains the short, spoken form.
function recordingCode(r) {
  return String((r && r.recording_number) || '').trim();
}

function codeChip(r) {
  const code = recordingCode(r);
  if (!code) return '';
  return `<span class="text-[11px] font-semibold tabular-nums text-mut">${esc(code)}</span>`;
}

function codeCopyButton(r) {
  const code = recordingCode(r);
  if (!code) return '';
  // A real <button>: Enter, Space and the tab order come from the platform.
  return `<span class="inline-flex items-center">
    <button type="button" data-copy-number="${esc(code)}"
      aria-label="Скопировать номер записи ${esc(code)}"
      class="inline-flex items-center gap-1.5 rounded-lg border border-line/60 bg-card px-2.5 py-1 text-[12.5px] font-semibold tabular-nums text-[#c9c3ff] active:scale-[.97]">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
      ${esc(code)}
    </button>
    <span data-copy-status role="status" aria-live="polite"
      class="ml-2 text-[12.5px] font-semibold text-emerald-300"></span>
  </span>`;
}

let _toastTimer = null;
function showCopyStatus(button, text, success) {
  const el = button && button.parentNode
    ? button.parentNode.querySelector('[data-copy-status]') : null;
  if (!el) return;
  if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
  el.className = `ml-2 text-[12.5px] font-semibold ${success ? 'text-emerald-300' : 'text-red-300'}`;
  el.innerHTML = esc(text);
  _toastTimer = setTimeout(() => {
    _toastTimer = null;
    if (el.isConnected) el.innerHTML = '';
  }, 1800);
}

function legacyCopyText(text) {
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.setAttribute('aria-hidden', 'true');
  field.style.position = 'fixed';
  field.style.opacity = '0';
  field.style.pointerEvents = 'none';
  document.body.appendChild(field);
  try {
    if (typeof field.focus === 'function') field.focus();
    field.select();
    if (typeof field.setSelectionRange === 'function') {
      field.setSelectionRange(0, field.value.length);
    }
    return document.execCommand('copy') === true;
  } catch (e) {
    return false;
  } finally {
    field.remove();
  }
}

async function copyRecordingCode(code, button) {
  const text = `Recordings MCP recording: ${code}`;
  // Clipboard API is unavailable on the app's current http:// tailnet origin.
  // In that case use the browser's synchronous copy command while the original
  // tap still owns user activation. A denied modern write remains a real error.
  if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
    if (legacyCopyText(text)) {
      showCopyStatus(button, 'Скопировано', true);
      return true;
    }
    showCopyStatus(button, 'Не скопировано', false);
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    showCopyStatus(button, 'Не скопировано', false);
    return false;
  }
  showCopyStatus(button, 'Скопировано', true);
  return true;
}

async function api(path) {
  const r = await fetch(path, { credentials: 'same-origin' });
  if (r.status === 401) { showAuthWall(); throw new Error('unauth'); }
  if (!r.ok) throw new Error('http ' + r.status);
  return r.json();
}

// ---------- mutations ----------
// Every write carries a CSRF token in a header a cross-site page cannot set.
// The session cookie is HttpOnly and SameSite=Lax, so this is the second lock,
// not the only one. The token is fetched once and re-fetched once on a 403,
// which is what a redeployed secret looks like from here.
let _csrf = null;
async function csrfToken(force) {
  if (_csrf && !force) return _csrf;
  const r = await fetch('/api/csrf', { credentials: 'same-origin' });
  if (r.status === 401) { showAuthWall(); throw new Error('unauth'); }
  if (!r.ok) throw new Error('http ' + r.status);
  const data = await r.json();
  _csrf = (data && data.csrf) || null;
  return _csrf;
}

async function apiPost(path, body) {
  const send = (token) => fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token || '' },
    body: JSON.stringify(body),
  });
  let r = await send(await csrfToken());
  if (r.status === 403) r = await send(await csrfToken(true));
  if (r.status === 401) { showAuthWall(); throw new Error('unauth'); }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    // The server sends one safe Russian sentence and nothing else; it is shown
    // verbatim rather than replaced by a guess about what went wrong.
    const err = new Error('http ' + r.status);
    err.messageRu = (data && data.error_ru) || 'Не удалось выполнить действие.';
    throw err;
  }
  return data;
}

async function apiDelete(path) {
  const send = (token) => fetch(path, {
    method: 'DELETE', credentials: 'same-origin',
    headers: { 'X-CSRF-Token': token || '' },
  });
  let r = await send(await csrfToken());
  if (r.status === 403) r = await send(await csrfToken(true));
  if (r.status === 401) { showAuthWall(); throw new Error('unauth'); }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    const err = new Error('http ' + r.status);
    err.messageRu = (data && data.error_ru) || 'Не удалось выполнить действие.';
    throw err;
  }
  return data;
}

function showToast(message) {
  let box = document.querySelector('[data-app-toast]');
  if (!box) {
    box = document.createElement('div');
    box.setAttribute('data-app-toast', '');
    box.setAttribute('role', 'status');
    box.className = 'fixed bottom-5 left-4 right-4 z-50 rounded-xl bg-card border border-line px-3 py-2 text-[13px]';
    document.body.appendChild(box);
  }
  box.textContent = message || '';
}

function showAuthWall() {
  // The single place the session can end. Killing the refresh loop here means
  // no code path can keep hammering /api behind a login wall.
  asrPoller.stop();
  app.innerHTML = `
  <div class="min-h-screen flex items-center justify-center px-8 text-center">
    <div class="fade-in">
      <div class="text-5xl mb-4">🔒</div>
      <h1 class="text-xl font-semibold mb-2">Требуется вход</h1>
      <p class="text-mut text-sm">Откройте приложение по вашей magic-ссылке.</p>
    </div>
  </div>`;
}

// ---------- live ASR refresh ----------
// Long transcriptions land block by block, so the numbers on screen go stale
// while the reader watches them. Exactly one poller and one visibility listener
// for the whole app; the rendering rules themselves live in asr-progress.js.
//
// Race safety: every navigation bumps `_navGen`, and every fetch remembers the
// generation it was issued under. A response that arrives after the user moved
// on is dropped instead of painting the previous screen over the current one.
let _navGen = 0;
let _route = { kind: 'feed', id: null, gen: 0 };

function stale(gen) { return gen !== _navGen; }

// Patch progress badges in place. Nothing else in the DOM is rebuilt, so an
// open <audio> keeps playing, the scroll position holds, and the detail view
// the reader has open stays open.
function patchAsrSlots(updates) {
  if (!updates || !updates.size) return 0;
  let patched = 0;
  for (const slot of document.querySelectorAll('[data-asr-id]')) {
    const html = updates.get(String(slot.getAttribute('data-asr-id')));
    if (html === undefined || slot.innerHTML === html) continue;
    slot.innerHTML = html;
    patched++;
  }
  return patched;
}

// What the poller refreshes: whichever route is on screen right now. Search and
// the auth wall report nothing, so they are simply not polled.
function asrFetch() {
  const gen = _navGen;
  if (_route.kind === 'feed') return renderFeed({ silent: true, gen });
  if (_route.kind === 'detail') return refreshDetail(_route.id, gen);
  return Promise.resolve(null);
}

const asrPoller = createAsrPoller({
  intervalMs: 15000,
  isHidden: () => document.visibilityState === 'hidden',
  fetchData: asrFetch,
});

// A hidden tab must not poll at all; coming back must not show stale numbers.
document.addEventListener('visibilitychange', () => { asrPoller.onVisibilityChange(); });

// ---------- id-carrying clicks ----------
// One delegated listener for every control whose behaviour depends on a
// recording id. The id travels as a data attribute and is read back out of the
// DOM, never interpolated into an inline handler: `esc()` escapes what makes an
// attribute value safe (& < > "), not what makes a JavaScript string literal
// safe (' and \), so an id carrying an apostrophe could otherwise close the
// onclick's string and have the rest of itself executed as code. Ids come from
// the archive, and nothing upstream promises they are alphanumeric.

// File inputs report a choice with 'change', not 'click', so the header's
// upload control needs its own delegated listener.
app.addEventListener('change', (event) => {
  const input = event.target.closest('[data-upload]');
  if (input) uploadRecording(input);
});

app.addEventListener('click', (event) => {
  const target = event.target;
  if (!target || typeof target.closest !== 'function') return;
  // Checked before the card: copying a code must never also open the
  // recording, and it must never touch location.
  const copy = target.closest('[data-copy-number]');
  if (copy) {
    copyRecordingCode(copy.getAttribute('data-copy-number'), copy);
    return;
  }
  const languageToggle = target.closest('[data-language-toggle]');
  if (languageToggle) { toggleLanguage(); return; }
  const summaryLanguageApply = target.closest('[data-summary-language-apply]');
  if (summaryLanguageApply) {
    const input = document.querySelector('[data-summary-language]');
    if (input && !setSummaryLanguage(input.value)) input.setAttribute('aria-invalid', 'true');
    return;
  }
  const loadMore = target.closest('[data-load-more]');
  if (loadMore) { renderFeed({ append: true, gen: _navGen }); return; }
  const archiveMore = target.closest('[data-archive-more]');
  if (archiveMore) { renderArchive(_navGen, true); return; }
  const summaryRetry = target.closest('[data-summary-retry]');
  if (summaryRetry) { retrySummary(summaryRetry.getAttribute('data-rec'), summaryRetry.getAttribute('data-language')); return; }
  const refresh = target.closest('[data-view-refresh]');
  if (refresh) { refreshCurrentView(refresh); return; }
  const sourcePoll = target.closest('[data-source-poll]');
  if (sourcePoll) { requestSourcePoll(sourcePoll); return; }
  const publicShare = target.closest('[data-public-share]');
  if (publicShare) {
    createPublicShare(publicShare.getAttribute('data-public-share'), publicShare);
    return;
  }
  const publicRevoke = target.closest('[data-public-revoke]');
  if (publicRevoke) {
    revokePublicShare(publicRevoke.getAttribute('data-public-revoke'), publicRevoke);
    return;
  }
  // Speaker controls first: they live inside the detail view, which carries no
  // recording-id card of its own, but an early return keeps that independent of
  // how the detail markup is nested later.
  const play = target.closest('[data-snippet-play]');
  if (play) {
    playSnippet(play.getAttribute('data-start-ms'), play.getAttribute('data-end-ms'));
    return;
  }
  const save = target.closest('[data-speakers-save]');
  if (save) {
    saveSpeakerNames(save.getAttribute('data-rec'));
    return;
  }
  const task = target.closest('[data-task-id]');
  if (task) {
    toggleTask(task, task.getAttribute('data-rec'), task.getAttribute('data-task-id'));
    return;
  }
  const reprocess = target.closest('[data-reprocess]');
  if (reprocess) {
    requestReprocess(reprocess, reprocess.getAttribute('data-rec'),
                     reprocess.getAttribute('data-reprocess'));
    return;
  }
  const comment = target.closest('[data-comment-add]');
  if (comment) {
    addComment(comment, comment.getAttribute('data-rec'));
    return;
  }
  const label = target.closest('[data-label-id]');
  if (label) {
    toggleRecordingLabel(label, label.getAttribute('data-rec'), label.getAttribute('data-label-id'));
    return;
  }

  if (target.closest('[data-label-catalog-create-toggle]')) { _labelCatalogueCreate = true; renderFeed(); return; }
  if (target.closest('[data-label-catalog-cancel]')) { _labelCatalogueCreate = false; renderFeed(); return; }
  if (target.closest('[data-label-catalog-edit]')) { _labelCatalogueEdit = !_labelCatalogueEdit; renderFeed(); return; }
  if (target.closest('[data-label-catalog-create]')) { createCatalogueLabel(); return; }
  const deleteLabel = target.closest('[data-label-delete]');
  if (deleteLabel) { deleteCatalogueLabel(deleteLabel.getAttribute('data-label-delete')); return; }
  const filter = target.closest('[data-label-filter]');
  if (filter) {
    const id = filter.getAttribute('data-label-filter');
    if (_labelFilters.has(id)) _labelFilters.delete(id); else _labelFilters.add(id);
    renderFeed();
    return;
  }
  const archive = target.closest('[data-archive-action]');
  if (archive) {
    archiveAction(archive.getAttribute('data-rec'), archive.getAttribute('data-archive-action'));
    return;
  }
  const localDelete = target.closest('[data-delete-local]');
  if (localDelete) {
    confirmLocalDelete(localDelete.getAttribute('data-rec'));
    return;
  }
  const map = target.closest('[data-mm-open]');
  if (map) {
    openMindmap(
      map.getAttribute('data-mm-open'),
      map.getAttribute('data-mm-revision') || '',
    );
    return;
  }
  const card = target.closest('[data-rec-id]');
  if (card) location.hash = '#/r/' + card.getAttribute('data-rec-id');
});

// Enter in a name field saves, the way every other single-field form on a phone
// behaves; Escape puts the field back to what was stored. Delegated for the same
// reason the clicks are: the fields are re-rendered on every poll.
app.addEventListener('keydown', (event) => {
  const target = event.target;
  if (!target || typeof target.closest !== 'function') return;
  const field = target.closest('[data-speaker-name]');
  if (!field) return;
  if (event.key === 'Enter') {
    if (event.preventDefault) event.preventDefault();
    const save = document.querySelector('[data-speakers-save]');
    if (save) saveSpeakerNames(save.getAttribute('data-rec'));
  } else if (event.key === 'Escape') {
    field.value = field.getAttribute('data-initial') || '';
  }
});

// ---------- header / nav ----------
function header(title, opts = {}) {
  const back = opts.back
    ? `<button onclick="location.hash='${opts.back}'" class="w-9 h-9 -ml-2 flex items-center justify-center rounded-full active:bg-white/5">
         <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#f2f2f5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
       </button>` : '<div class="w-7"></div>';
  const refresh = `<button type="button" data-view-refresh aria-label="Обновить" title="Обновить" class="min-w-11 min-h-11 inline-flex items-center justify-center rounded-full active:bg-white/5 disabled:opacity-50"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#f2f2f5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg></button>`;
  const sourcePoll = opts.sourcePoll
    ? `<button type="button" data-source-poll aria-label="Проверить PLAUD" title="Проверить PLAUD" class="min-w-11 min-h-11 inline-flex items-center justify-center rounded-full active:bg-white/5 disabled:opacity-50"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#f2f2f5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 15v4h14v-4"/></svg></button>`
    : '';
  // Upload sits next to the PLAUD check: both add a recording to the same feed,
  // one from the recorder and one from the phone you are holding. A real file
  // input, so the phone offers its own recorder and files app.
  const upload = opts.sourcePoll
    ? `<label data-upload-label aria-label="Загрузить запись" title="Загрузить запись" class="min-w-11 min-h-11 inline-flex cursor-pointer items-center justify-center rounded-full active:bg-white/5"><input type="file" data-upload accept="audio/*,video/*" class="sr-only"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#f2f2f5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V7"/><path d="m7 12 5-5 5 5"/><path d="M5 19v2h14v-2"/></svg></label>`
    : '';
  const languageToggle = languageToggleHtml();
  const right = opts.search
    ? `<div data-header-actions class="flex shrink-0 items-center gap-0.5">${languageToggle}${refresh}${upload}${sourcePoll}<button data-archive-nav aria-label="Архив" title="Архив" onclick="location.hash='#/archive'" class="min-w-11 min-h-11 inline-flex items-center justify-center rounded-full active:bg-white/5"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#f2f2f5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M5 6l1 14h12l1-14"/><path d="M9 10h6"/><path d="M4 3h16v3H4z"/></svg></button><button aria-label="Поиск" onclick="location.hash='#/search'" class="min-w-11 min-h-11 -mr-2 flex items-center justify-center rounded-full active:bg-white/5">
         <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#f2f2f5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>
       </button></div>` : `<div data-header-actions class="flex shrink-0 items-center">${languageToggle}${refresh}${upload}${sourcePoll}</div>`;
  const sourceStatus = opts.sourcePoll
    ? `<p data-source-poll-status role="status" aria-live="polite" class="absolute right-4 top-full max-w-[calc(100vw-2rem)] rounded-b-lg bg-card px-2 py-1 text-[12px] text-mut shadow-lg empty:hidden">${esc(_sourcePollStatus)}</p>`
    : '';
  return `<header class="safe-t sticky top-0 z-20 bg-ink/85 backdrop-blur-xl border-b border-line/60">
      <div class="relative flex items-center justify-between px-4 h-14">
        ${back}
        <h1 class="flex-1 text-center text-[17px] font-semibold truncate px-2">${esc(tr(title))}</h1>
        ${right}
        ${sourceStatus}
      </div>
    </header>`;
}

let _sourcePollStatus = '';
let _manualRefresh = false;
async function refreshCurrentView(button) {
  if (_manualRefresh) return;
  _manualRefresh = true; button.disabled = true; button.classList.add('animate-spin');
  try {
    const gen = _navGen;
    if (_route.kind === 'feed') await renderFeed({ gen, force: true });
    else if (_route.kind === 'detail') await refreshDetail(_route.id, gen);
    else if (_route.kind === 'archive') await renderArchive();
    else if (_route.kind === 'search') await renderSearch();
  } finally {
    _manualRefresh = false;
    if (button.isConnected) { button.disabled = false; button.classList.remove('animate-spin'); }
  }
}

async function requestSourcePoll(button) {
  if (button.disabled) return;
  button.disabled = true;
  const setStatus = (message) => {
    _sourcePollStatus = message;
    const status = document.querySelector('[data-source-poll-status]');
    if (status) status.innerHTML = esc(message);
  };
  setStatus('Проверка PLAUD запущена');
  try {
    await apiPost('/api/source/poll', {});
    setTimeout(() => refreshCurrentView(document.querySelector('[data-view-refresh]') || button), 1800);
  } catch (e) {
    setStatus(e.messageRu || 'Не удалось запустить проверку PLAUD.');
  } finally {
    setTimeout(() => { if (button.isConnected) button.disabled = false; }, 1800);
  }
}

async function uploadRecording(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const label = input.closest('[data-upload-label]');
  const setStatus = (message) => {
    _sourcePollStatus = message;
    const status = document.querySelector('[data-source-poll-status]');
    if (status) status.innerHTML = esc(message);
  };
  // Transcoding a long recording takes a while and the feed will not show it
  // until the connector is done, so say so instead of appearing to hang.
  setStatus(`Загружаю «${file.name}»…`);
  if (label) label.classList.add('opacity-50', 'pointer-events-none');
  try {
    const send = (token) => fetch('/api/uploads', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': token || '', 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    let r = await send(await csrfToken());
    if (r.status === 403) r = await send(await csrfToken(true));
    if (r.status === 401) { showAuthWall(); return; }
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) { setStatus((data && data.message_ru) || 'Не удалось загрузить файл.'); return; }
    setStatus((data && data.message_ru) || 'Запись загружена');
    refreshCurrentView(document.querySelector('[data-view-refresh]'));
  } catch (e) {
    setStatus('Не удалось загрузить файл.');
  } finally {
    // Always clear: picking the same file twice must fire change again.
    input.value = '';
    if (label) label.classList.remove('opacity-50', 'pointer-events-none');
  }
}

function skeletonFeed() {
  let s = '';
  for (let i = 0; i < 5; i++) {
    s += `<div class="rounded-2xl h-[92px] mb-3 shimmer"></div>`;
  }
  return `<div class="px-4 pt-4">${s}</div>`;
}

// ---------- feed ----------
// What the rendered feed is showing, so a silent refresh can tell "same cards,
// new progress" (patch in place) from "the list itself changed" (rebuild once).
let _feedKey = null;
const _labelFilters = new Set();
let _labelCatalogueCreate = false;
let _labelCatalogueEdit = false;
let _feedRows = [];
let _feedCursor = null;
let _archiveCursor = null;
let _archiveRows = [];

function labelTitle(defs, id) {
  const item = (Array.isArray(defs) ? defs : []).find(value => value && value.id === id);
  return (item && item.name) || id;
}

async function renderFeed(opts = {}) {
  const gen = opts.gen == null ? _navGen : opts.gen;
  if (!opts.silent && !opts.append) { _feedCursor = null; _feedRows = []; app.innerHTML = localized(header('Записи', { search: true, sourcePoll: true }) + skeletonFeed()); }
  let payload;
  const cursorPart = opts.append && _feedCursor ? '&cursor=' + encodeURIComponent(_feedCursor) : '';
  try { payload = await api('/api/recordings?page=cursor' + cursorPart + languageQuery()); }
  catch (e) {
    // Every failure returns null rather than throwing: null tells the poller
    // "nothing new", so a transient error retries on the next tick instead of
    // killing the loop. showAuthWall() already stopped it if this was a 401.
    if (e.message !== 'unauth' && !opts.silent && !stale(gen)) {
      app.innerHTML = localized(header('Записи', { search: true, sourcePoll: true }) + errBox('Не удалось загрузить записи'));
    }
    return null;
  }
  if (stale(gen)) return null;   // the reader navigated away while this loaded
  if (!Array.isArray(payload)) setDisplayTimezone(payload && payload.display_timezone);
  const pageRows = Array.isArray(payload) ? payload : ((payload && payload.items) || []);
  const nextCursor = Array.isArray(payload) ? null : ((payload && payload.next_cursor) || null);
  let rows;
  if (opts.append) rows = _feedRows.concat(pageRows.filter(row => !_feedRows.some(old => old.id === row.id)));
  else if (opts.silent && _feedRows.length > pageRows.length) rows = pageRows.concat(_feedRows.filter(old => !pageRows.some(row => row.id === old.id)));
  else rows = pageRows;
  _feedRows = rows; _feedCursor = nextCursor;

  // Labels remain in API payloads and on recording cards/details. The feed no
  // longer renders or applies label filters; search is the discovery UI.

  const key = feedCardsKey(rows);
  if (opts.silent && !opts.force && key === _feedKey) {
    patchAsrSlots(asrSlotUpdates(rows));   // same cards: only the badges move
    asrPoller.note(rows);
    return rows;
  }
  _feedKey = key;

  if (!rows.length) {
    app.innerHTML = localized(header('Записи', { search: true, sourcePoll: true }) + emptyBox('🎙️','Пока нет записей','Записи появятся здесь после синхронизации.'));
    asrPoller.note(rows);
    return rows;
  }

  const groups = [];
  let cur = null;
  for (const r of rows) {
    const d = recordingDate(r);
    const lab = groupLabel(d);
    if (!cur || cur.label !== lab) { cur = { label: lab, items: [] }; groups.push(cur); }
    cur.items.push({ r, d });
  }

  let html = header('Записи', { search: true, sourcePoll: true }) + `<div class="px-4 pt-3 pb-24 fade-in">`;
  for (const g of groups) {
    html += `<div class="text-[13px] font-semibold text-mut mt-5 mb-2 px-1">${esc(g.label)}</div>`;
    for (const { r, d } of g.items) {
      const summary = r.summary ? `<p class="text-[13px] text-mut mt-1 leading-snug line-clamp-1">${esc(r.summary)}</p>` : '';
      const labelChips = (r.labels || []).map(id => `<span class="rounded-full bg-accent/15 text-[#c9c3ff] px-2 py-0.5 text-[10px]">${esc(labelTitle(r.label_definitions,id))}</span>`).join('');
      html += `
      <button data-rec-id="${esc(r.id)}" class="w-full text-left block bg-card rounded-2xl px-4 py-3.5 mb-2.5 border border-line/50 active:scale-[.985] transition-transform">
        <div class="flex items-start gap-3">
          <div class="mt-0.5 w-9 h-9 shrink-0 rounded-xl bg-accent/15 flex items-center justify-center">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a99bff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><path d="M12 18v4"/></svg>
          </div>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <h3 class="font-semibold text-[15px] leading-tight truncate flex-1">${esc(r.title || r.name)}</h3>
            </div>
            ${summary}
            ${labelChips ? `<div class="flex flex-wrap gap-1 mt-2">${labelChips}</div>` : ''}
            <div class="flex items-center gap-2 mt-1.5 text-[12px] text-mut">
              ${codeChip(r)}
              <span>${timeLabel(d)}</span>
              ${r.duration_ms ? `<span class="w-1 h-1 rounded-full bg-mut/50"></span><span>${fmtDur(r.duration_ms)}</span>` : ''}
              ${r.lang ? '<span class="w-1 h-1 rounded-full bg-mut/50"></span>' + langBadge(r.lang) : ''}
            </div>
            <div class="mt-2 empty:hidden" data-asr-id="${esc(r.id)}">${asrProgressHtml(r.asr_processing)}</div>
          </div>
        </div>
      </button>`;
    }
  }
  if (_feedCursor) html += `<button type="button" data-load-more class="mx-auto mt-4 flex min-h-11 items-center justify-center rounded-xl border border-line/60 px-5 text-[14px] font-semibold">${tr('Показать ещё')}</button>`;
  html += `</div>`;
  app.innerHTML = localizedPreserving(html, rows.flatMap(r => [r.title, r.name, r.source_name, r.summary]));
  asrPoller.note(rows);
  return rows;
}

function errBox(msg) {
  return `<div class="px-8 pt-24 text-center text-mut fade-in"><div class="text-4xl mb-3">⚠️</div><p>${esc(msg)}</p></div>`;
}
function emptyBox(icon, title, sub) {
  return `<div class="px-8 pt-28 text-center fade-in"><div class="text-5xl mb-4">${icon}</div><h2 class="text-lg font-semibold mb-1">${esc(title)}</h2><p class="text-mut text-sm">${esc(sub)}</p></div>`;
}

// ---------- detail (PLAUD-style tabs) ----------
let _detail = null;
let _detailTab = 'summary';

// distinct accent colors per speaker (assigned by order of first appearance)
const SPEAKER_PALETTE = [
  '#7c6cf0', '#4db6ff', '#34d399', '#fbbf24', '#f472b6', '#22d3ee',
  '#a3e635', '#c084fc', '#fb923c', '#2dd4bf', '#f87171', '#38bdf8',
];
function speakerLabelRu(name) {
  const m = /^\s*speaker\s*(\d+)\s*$/i.exec(name || '');
  if (m) return `Спикер ${m[1]}`;
  return name || 'Спикер';
}

// Seek the (persistent) audio player to a ms offset — PLAUD tap-to-jump.
function seekAudio(ms) {
  const a = document.getElementById('rec-audio');
  if (!a) return;
  const t = Math.max(0, (ms || 0) / 1000);
  const go = () => { try { a.currentTime = t; a.play().catch(() => {}); } catch (e) {} };
  if (a.readyState >= 1) go();
  else {
    a.preload = 'metadata';
    a.addEventListener('loadedmetadata', go, { once: true });
    try { a.load(); } catch (e) {}
  }
  try { a.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) {}
}

// group consecutive same-speaker segments into speaker turns, color-coded.
// The heading is the name the reader assigned, when there is one; the raw label
// the segment carries is never rewritten, only presented differently.
function segmentSpeakerLabel(s) {
  return (s && s.display_name) || speakerLabelRu(s && s.speaker);
}

function buildSegmentsHtml(segments) {
  const order = {};
  let n = 0;
  for (const s of segments) {
    const k = s.speaker_id || s.speaker || '?';
    if (!(k in order)) order[k] = n++;
  }
  const turns = [];
  let cur = null;
  for (const s of segments) {
    const k = s.speaker_id || s.speaker || '?';
    if (!cur || cur.speaker !== k) {
      cur = { speaker: k, label: segmentSpeakerLabel(s), start_ms: s.start_ms,
              text: s.text };
      turns.push(cur);
    } else {
      cur.text += ' ' + s.text;
    }
  }
  let html = '<div class="space-y-4">';
  for (const t of turns) {
    const c = SPEAKER_PALETTE[order[t.speaker] % SPEAKER_PALETTE.length];
    const label = t.label;
    const ts = fmtTs(t.start_ms);
    const tsBtn = ts
      ? `<button onclick="seekAudio(${Number(t.start_ms) || 0})" class="inline-flex items-center gap-1 text-[12px] text-mut font-medium tabular-nums px-1.5 py-0.5 rounded-md active:bg-white/5 active:text-accent">
           <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4l14 8-14 8z"/></svg>${ts}
         </button>`
      : '';
    html += `
      <div>
        <div class="flex items-center gap-2 mb-1">
          <span class="text-[12px] font-semibold px-2 py-0.5 rounded-full" style="color:${c};background:${c}22">${esc(label)}</span>
          ${tsBtn}
        </div>
        <p class="text-[15.5px] leading-relaxed text-[#e7e7ee]">${esc(t.text)}</p>
      </div>`;
  }
  html += '</div>';
  return html;
}

// {raw source label: assigned name}, from the speaker model the API sends.
function speakerLabelNames(r) {
  const list = (r && r.speakers && r.speakers.speakers) || [];
  const map = {};
  for (const s of list) {
    if (s && s.source_label && s.display_name) map[s.source_label] = s.display_name;
  }
  return map;
}

// "[Speaker 1] текст" -> "[Аня] текст", for the flat-transcript fallback.
// Only a bracketed label at the start of a line is touched: prose that mentions
// a label is not a speaker turn, and this is presentation, not a rewrite.
function applyNamesToTranscript(text, labelNames) {
  if (!text || !labelNames || !Object.keys(labelNames).length) return text;
  return text.split('\n').map((line) => {
    const m = /^(\s*)\[([^\]\n]*)\](.*)$/.exec(line);
    if (!m) return line;
    const name = labelNames[m[2]] || labelNames[m[2].trim()];
    return name ? `${m[1]}[${name}]${m[3]}` : line;
  }).join('\n');
}

function transcriptTabHtml(r) {
  if (r.segments && r.segments.length) return buildSegmentsHtml(r.segments);
  const asr = (r.asr_transcript || '').trim();
  const plaud = (r.plaud_transcript || '').trim();
  const flat = applyNamesToTranscript(asr || plaud, speakerLabelNames(r));
  if (flat) {
    const note = asr
      ? `<div class="text-[12px] text-mut mb-3 flex items-center gap-1.5">
           <span class="w-1.5 h-1.5 rounded-full bg-accent/70"></span>расшифровка Barston ASR
         </div>`
      : '';
    const paras = flat.split(/\n{2,}|\n/).map(p => p.trim()).filter(Boolean);
    const body = paras
      .map(p => `<p class="text-[15.5px] leading-relaxed text-[#e7e7ee] mb-3">${esc(p)}</p>`)
      .join('');
    return localized(note) + body;
  }
  if (r.asr_processing) {
    // Real block-by-block progress instead of an indefinite "обрабатывается…".
    return localized(`<div class="bg-card border border-line/50 rounded-2xl p-4"
              ><div data-asr-id="${esc(r.id)}">${asrProgressHtml(r.asr_processing)}</div></div>`);
  }
  return localized(`<div class="text-mut text-sm bg-card border border-line/50 rounded-2xl p-4">Транскрипт обрабатывается…</div>`);
}

// ---------- speakers tab ----------
// Truthful by construction: everything rendered here comes from the API's
// speaker model, which is derived from real segment labels. When there are none,
// this tab says so — it never shows a count nobody measured.

// A snippet is a few seconds to recognise a voice by. The server already bounds
// the interval; this is the client-side half of the same promise, so a hand-
// edited data attribute still cannot turn the button into "play the whole file".
const SNIPPET_MAX_S = 12;

// The one in-flight snippet. Kept in module scope so a second tap, a tab
// switch, or a poll-driven re-render can always cancel the listener it added.
let _snippet = null;

function stopSnippet() {
  const active = _snippet;
  _snippet = null;
  if (!active) return;
  try { active.el.removeEventListener('timeupdate', active.handler); } catch (e) {}
  try { active.el.pause(); } catch (e) {}
}

// Play [start, end) of the recording through the player already on the page.
// That element streams the private, authenticated, no-store /audio/{id} endpoint
// with Range requests, so nothing here creates a clip, a blob or a public URL,
// and nothing new enters any cache.
function playSnippet(startMs, endMs) {
  const el = document.getElementById('rec-audio');
  if (!el) return;
  stopSnippet();
  const start = Math.max(0, Number(startMs) || 0) / 1000;
  const rawEnd = Number(endMs);
  const end = Math.min(isFinite(rawEnd) ? rawEnd / 1000 : start + SNIPPET_MAX_S,
                       start + SNIPPET_MAX_S);
  if (!(end > start)) return;
  const handler = () => {
    // A 20 ms margin: timeupdate fires every ~250 ms, so waiting for a sample
    // exactly past the boundary would overrun the snippet audibly.
    if (el.currentTime >= end - 0.02) stopSnippet();
  };
  _snippet = { el, handler, end };
  el.addEventListener('timeupdate', handler);
  const go = () => {
    try {
      el.currentTime = start;
      const played = el.play();
      if (played && played.catch) played.catch(() => {});
    } catch (e) {}
  };
  if (el.readyState >= 1) go();
  else {
    el.preload = 'metadata';
    el.addEventListener('loadedmetadata', go, { once: true });
    try { el.load(); } catch (e) {}
  }
}

function speakerCountRu(n) {
  if (uiLanguage === 'en') return `${n} ${n === 1 ? 'speaker' : 'speakers'}`;
  return `${n} ${plural(n, 'спикер', 'спикера', 'спикеров')}`;
}

function speakerRowHtml(s, index) {
  const color = SPEAKER_PALETTE[index % SPEAKER_PALETTE.length];
  const shown = s.name_ru || speakerLabelRu(s.source_label);
  const name = s.display_name || '';
  const voiceprint = s.voiceprint && s.voiceprint.identity ? s.voiceprint : null;
  const confidence = voiceprint && Number(voiceprint.confidence);
  const confidencePct = Number.isFinite(confidence)
    ? ` · ${Math.round(Math.max(0, Math.min(1, confidence)) * 100)}%`
    : '';
  const voiceprintBadge = voiceprint
    ? `<span data-voiceprint-identity class="text-[11.5px] font-semibold text-emerald-300">Распознано: ${esc(voiceprint.identity)}${confidencePct}</span>`
    : '';
  const snippet = s.snippet;
  // 44px targets: this is used one-handed on a phone.
  const play = snippet
    ? `<button data-snippet-play data-start-ms="${esc(String(snippet.start_ms))}"
            data-end-ms="${esc(String(snippet.end_ms))}"
            aria-label="Прослушать фрагмент: ${esc(shown)}"
            class="w-11 h-11 shrink-0 rounded-xl border border-line/60 bg-panel flex items-center justify-center active:scale-95 transition-transform">
         <svg width="16" height="16" viewBox="0 0 24 24" fill="${color}" aria-hidden="true"><path d="M6 4l14 8-14 8z"/></svg>
       </button>`
    : `<span class="text-[11.5px] text-mut w-11 shrink-0 text-center leading-tight">Фрагмент недоступен</span>`;
  const turns = uiLanguage === 'en' ? `${s.segment_count} ${s.segment_count === 1 ? 'turn' : 'turns'}` : `${s.segment_count} ${plural(s.segment_count, 'реплика', 'реплики', 'реплик')}`;
  return `
    <div data-speaker-row data-speaker-id="${esc(s.speaker_id)}"
         class="rounded-2xl border border-line/50 bg-card p-3.5 mb-2.5">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-[12px] font-semibold px-2 py-0.5 rounded-full"
              style="color:${color};background:${color}22">${esc(shown)}</span>
        <span class="text-[11.5px] text-mut">метка: ${esc(s.source_label)}</span>
        ${voiceprintBadge}
      </div>
      <div class="flex items-center gap-2 mt-2.5">
        <input data-speaker-name="${esc(s.speaker_id)}" data-initial="${esc(name)}"
               value="${esc(name)}" maxlength="60" type="text"
               autocomplete="off" autocapitalize="words" enterkeyhint="done"
               placeholder="${esc(shown)}"
               aria-label="Имя спикера с меткой ${esc(s.source_label)}"
               class="flex-1 min-w-0 h-11 bg-panel border border-line/60 rounded-xl px-3 text-[15px] text-white placeholder-mut outline-none focus:border-accent" />
        ${play}
      </div>
      <div class="text-[11.5px] text-mut mt-1.5">${esc(turns)} · ${esc(fmtDur(s.total_ms))}</div>
      ${typeof identitySelectorHtml === 'function' ? identitySelectorHtml(s) : ''}
    </div>`;
}

// What the durable queue is doing for this recording, in the reader's language.
// Server-supplied text only: the worker's own diagnosis never leaves the archive.
const JOB_TITLE_RU = {
  retranscribe: 'Перетранскрибирование',
  derive: 'Пересборка материалов',
  diarization: 'Разметка спикеров',
};

function jobsHtml(r) {
  const jobs = (r && r.jobs) || {};
  const lines = Object.keys(jobs)
    .filter((kind) => jobs[kind] && jobs[kind].message_ru)
    .map((kind) => `<div>${esc(JOB_TITLE_RU[kind] || kind)}: ${esc(jobs[kind].message_ru)}</div>`);
  return lines.join('');
}

function reprocessHtml(r) {
  const button = (action, label) =>
    `<button data-reprocess="${action}" data-rec="${esc(r.id)}"
        class="flex-1 min-h-11 rounded-xl border border-line/60 bg-panel px-3 py-2.5 text-[14px] font-semibold text-white active:scale-[.99] transition-transform">
       ${label}
     </button>`;
  return `
    <div class="mt-5 rounded-2xl border border-line/50 bg-card p-4">
      <div class="text-[13px] font-semibold mb-1">Обработка</div>
      <p class="text-[12px] text-mut mb-3 leading-snug">
        Задача выполняется в фоне. Текущий транскрипт, резюме и карта остаются на месте,
        пока не появится новая готовая версия.
      </p>
      <div class="flex flex-col sm:flex-row gap-2">
        ${button('transcript', 'Перетранскрибировать')}
        ${button('materials', 'Пересобрать материалы')}
        ${button('diarize', 'Распознать спикеров')}
      </div>
      <div data-reprocess-status aria-live="polite"
           class="text-[12px] text-mut mt-3 space-y-0.5 empty:hidden">${jobsHtml(r)}</div>
    </div>`;
}

function speakersTabHtml(r) {
  const model = (r && r.speakers) || {};
  const list = Array.isArray(model.speakers) ? model.speakers : [];
  if (!list.length) {
    // Honest empty state. No count, no invented speaker — just what is known.
    const note = model.note_ru
      || 'Разметка спикеров недоступна для этой записи.';
    return `
      <div class="fade-in">
        <div class="rounded-2xl border border-line/50 bg-card p-4 text-[13.5px] text-mut leading-snug">
          ${esc(note)}
        </div>
        ${reprocessHtml(r)}
      </div>`;
  }
  const truncated = model.truncated
    ? `<div class="text-[11.5px] text-mut mb-2 px-1">Показаны первые ${esc(String(list.length))}.</div>`
    : '';
  const canEdit = model.can_edit === false ? false : true;
  const save = canEdit
    ? `<div class="flex items-center gap-3 mt-1">
         <button data-speakers-save data-rec="${esc(r.id)}"
           class="min-h-11 rounded-xl bg-accent/90 px-4 py-2.5 text-[14px] font-semibold text-white active:scale-[.99] transition-transform">
           Сохранить имена
         </button>
         <div data-speakers-status role="status" aria-live="polite"
              class="text-[12px] text-mut flex-1 min-w-0"></div>
       </div>`
    : `<div data-speakers-status role="status" aria-live="polite"
             class="text-[12px] text-mut">Сохранение имён сейчас недоступно.</div>`;
  return `
    <div class="fade-in">
      <div class="text-[13px] text-mut mb-3 px-1">${esc(speakerCountRu(list.length))} · имена сохраняются только у вас</div>
      ${truncated}
      <div data-speakers-list>${list.map(speakerRowHtml).join('')}</div>
      ${save}
      ${reprocessHtml(r)}
    </div>`;
}

// ---------- speaker mutations ----------

function setStatus(selector, text) {
  const box = document.querySelector(selector);
  if (box) box.innerHTML = esc(text);
}

async function saveSpeakerNames(recId) {
  const inputs = document.querySelectorAll('[data-speaker-name]');
  const names = {};
  let changed = 0;
  for (const input of inputs) {
    const id = input.getAttribute('data-speaker-name');
    const before = input.getAttribute('data-initial') || '';
    const now = (input.value == null ? '' : String(input.value)).trim();
    if (id && now !== before) { names[id] = now; changed++; }
  }
  if (!changed) { setStatus('[data-speakers-status]', 'Изменений нет.'); return; }
  setStatus('[data-speakers-status]', 'Сохранение…');
  try {
    const result = await apiPost(
      `/api/recordings/${encodeURIComponent(recId)}/speakers`, { names });
    if (_detail && _detail.id === recId && result) {
      // Re-render from the server's own view of the names, so what is on screen
      // is what was stored — including a name the server trimmed.
      if (result.speakers) _detail.speakers = Object.assign({}, _detail.speakers,
        { speakers: result.speakers, count: result.speakers.length });
      if (result.jobs) _detail.jobs = result.jobs;
      const map = {};
      for (const s of result.speakers || []) {
        if (s.display_name) map[s.speaker_id] = s.display_name;
      }
      _detail.speaker_names = map;
      _detail.segments = (_detail.segments || []).map((s) => Object.assign({}, s, {
        display_name: (s.speaker_id && map[s.speaker_id]) || null,
      }));
      showTab('speakers');
    }
    setStatus('[data-speakers-status]',
      (result && result.status_ru) || 'Имена сохранены.');
  } catch (e) {
    if (e.message === 'unauth') return;
    // The typed text is deliberately left in the field: a failed save must not
    // cost the reader what they wrote.
    setStatus('[data-speakers-status]', e.messageRu || 'Не удалось сохранить имена.');
  }
}

async function requestReprocess(button, recId, action) {
  if (!button || button.hasAttribute('disabled')) return;
  // The server is idempotent per (recording, action); this only stops the same
  // tap being sent twice while the first request is still in the air.
  button.setAttribute('disabled', '');
  setStatus('[data-reprocess-status]', 'Отправляем запрос…');
  try {
    const result = await apiPost(
      `/api/recordings/${encodeURIComponent(recId)}/reprocess`, { action });
    if (_detail && _detail.id === recId && result && result.jobs) {
      _detail.jobs = result.jobs;
    }
    setStatus('[data-reprocess-status]',
      (result && result.message_ru) || 'Запрос принят.');
  } catch (e) {
    if (e.message === 'unauth') return;
    setStatus('[data-reprocess-status]',
      e.messageRu || 'Не удалось отправить запрос.');
  } finally {
    const live = document.querySelector(`[data-reprocess="${action}"]`);
    if (live) live.removeAttribute('disabled');
  }
}

// ---------- recording labels ----------
async function createCatalogueLabel() {
  const input = document.querySelector('[data-label-new-name]');
  const button = document.querySelector('[data-label-catalog-create]');
  const status = document.querySelector('[data-label-catalog-status]');
  const name = (input && input.value || '').trim();
  if (!name) {
    if (status) status.innerHTML = esc('Введите название метки.');
    return;
  }
  if (!button || button.hasAttribute('disabled')) return;
  button.setAttribute('disabled', '');
  if (status) status.innerHTML = esc('Создание…');
  try {
    const result = await apiPost('/api/labels', {name});
    if (_feedRows && result.label) _feedRows.forEach(row => row.label_definitions = [...(row.label_definitions || []), result.label]);
    _labelCatalogueCreate = false;
    renderFeed();
  } catch (e) {
    // Do not re-render here: preserving the typed name lets the reader correct
    // it or retry after a transient control/socket failure.
    if (e.message !== 'unauth' && status) status.innerHTML = esc(e.messageRu || 'Не удалось создать метку.');
  } finally {
    if (button.isConnected) button.removeAttribute('disabled');
  }
}
function deleteCatalogueLabel(id) {
  const def = (_feedRows && _feedRows[0] && _feedRows[0].label_definitions || []).find(x => x.id === id);
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
  dialog.innerHTML = `<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"><div class="rounded-2xl bg-card p-4"><p>Удалить метку «${esc(def && def.name || '')}»? Она будет удалена из всех записей.</p><button type="button" data-delete-confirm>Удалить</button><button type="button" data-delete-cancel>Отмена</button></div></div>`;
  document.body.appendChild(dialog);
  dialog.addEventListener('click', async e => {
    if (e.target.closest('[data-delete-cancel]')) dialog.remove();
    if (!e.target.closest('[data-delete-confirm]')) return;
    try { await apiDelete(`/api/labels/${encodeURIComponent(id)}`); _labelFilters.delete(id); (_feedRows || []).forEach(row => { row.label_definitions=(row.label_definitions||[]).filter(x=>x.id!==id); row.labels=(row.labels||[]).filter(x=>x!==id); }); dialog.remove(); renderFeed(); }
    catch (err) { showToast(err.messageRu || 'Не удалось удалить метку.'); }
  });
}
function recordingLabelsHtml(r) {
  const definitions = Array.isArray(r && r.label_definitions) ? r.label_definitions : [];
  const active = new Set(Array.isArray(r && r.labels) ? r.labels : []);
  const controls = definitions.map((label) => {
    if (!label || !label.id) return '';
    const selected = active.has(label.id);
    return `<button type="button" role="checkbox" aria-checked="${selected ? 'true' : 'false'}"
      data-label-id="${esc(String(label.id))}" data-rec="${esc(r.id)}"
      class="min-h-11 rounded-full border px-3 text-[13px] font-semibold ${selected ? 'bg-accent border-accent text-white' : 'bg-panel border-line text-mut'}">
      ${esc(label.name || label.id)}${selected ? ' ✓' : ''}</button>`;
  }).join('');
  return `<section data-recording-labels class="mt-4 rounded-2xl border border-line/50 bg-card p-3.5">
    <h2 class="text-[15px] font-semibold mb-3">Метки</h2>
    <p class="-mt-2 mb-3 text-[12px] text-mut">Можно выбрать несколько.</p>
    <div class="flex flex-wrap gap-2">${controls}</div>
    <p class="mt-3 text-[12px] text-mut">Новые фильтры создаются на главной странице.</p>
    <div data-label-status aria-live="polite" class="text-[12px] text-mut mt-2"></div>
  </section>`;
}

async function toggleRecordingLabel(button, recId, labelId) {
  if (!button || button.hasAttribute('disabled')) return;
  const active = button.getAttribute('aria-checked') !== 'true';
  button.setAttribute('disabled', '');
  try {
    const result = await apiPost(`/api/recordings/${encodeURIComponent(recId)}/labels`, { label_id: labelId, active });
    const label = result && result.label;
    if (_detail && _detail.id === recId && label) {
      const labels = new Set(Array.isArray(_detail.labels) ? _detail.labels : []);
      if (label.active) labels.add(label.id); else labels.delete(label.id);
      _detail.labels = [...labels];
      const live = document.querySelector('[data-recording-labels]');
      if (live) live.innerHTML = recordingLabelsHtml(_detail).replace(/^<section[^>]*>|<\/section>$/g, '');
    }
  } catch (e) {
    if (e.message !== 'unauth') showToast(e.messageRu || 'Не удалось изменить метку.');
  }
}


// ---------- generated task mutations ----------
async function toggleTask(button, recId, taskId) {
  if (!button || button.hasAttribute('disabled')) return;
  const completed = button.getAttribute('aria-checked') !== 'true';
  button.setAttribute('disabled', '');
  try {
    const result = await apiPost(
      `/api/recordings/${encodeURIComponent(recId)}/tasks/${encodeURIComponent(taskId)}`, { completed });
    const task = result && result.task;
    if (_detail && _detail.id === recId && task) {
      _detail.tasks = (Array.isArray(_detail.tasks) ? _detail.tasks : []).map((item) =>
        String(item.id) === String(task.id) ? Object.assign({}, item, { completed: !!task.completed }) : item);
      if (_detail.summary_card_data && Array.isArray(_detail.summary_card_data.tasks)) {
        _detail.summary_card_data.tasks = _detail.summary_card_data.tasks.map((item) =>
          String(item.id) === String(task.id) ? Object.assign({}, item, { completed: !!task.completed }) : item);
      }
      showTab(_detailTab);
    }
  } catch (e) {
    if (e.message !== 'unauth') showToast(e.messageRu || 'Не удалось обновить задачу.');
  } finally {
    const live = document.querySelector(`[data-task-id="${taskId}"]`);
    if (live) live.removeAttribute('disabled');
  }
}

// ---------- comments ----------
function commentDateLabel(value) {
  const date = parseDate(value);
  const parts = zonedParts(date);
  return parts ? `${parts.day} ${MONTHS[parts.month - 1]}, ${timeLabel(date)}` : '';
}

function commentsHtml(r) {
  const comments = Array.isArray(r && r.comments) ? r.comments : [];
  const rows = comments.length
    ? comments.map((comment) => {
        const date = commentDateLabel(comment && (comment.created_at_utc || comment.created_at));
        return `<div data-comment-id="${esc(String((comment && comment.id) || ''))}" class="rounded-xl border border-line/50 bg-card p-3">
          <div class="text-[14px] leading-relaxed whitespace-pre-wrap">${esc(comment && comment.text)}</div>
          ${date ? `<div class="text-[11px] text-mut mt-1.5">${esc(date)}</div>` : ''}
        </div>`;
      }).join('')
    : '<div class="text-[13px] text-mut">Комментариев пока нет.</div>';
  return `<section data-comments class="mt-5 border-t border-line/50 pt-5">
    <h2 class="text-[15px] font-semibold mb-3">Комментарии</h2>
    <div data-comments-list class="space-y-2.5 mb-3">${rows}</div>
    <textarea data-comment-text maxlength="2000" rows="3" aria-label="Новый комментарий"
      placeholder="Добавить комментарий"
      class="w-full resize-y bg-panel border border-line/60 rounded-xl px-3 py-2.5 text-[14px] text-white placeholder-mut outline-none focus:border-accent"></textarea>
    <div class="mt-2 flex items-center gap-3">
      <button type="button" data-comment-add data-rec="${esc(r.id)}"
        class="min-h-11 rounded-xl bg-accent/90 px-4 py-2.5 text-[14px] font-semibold text-white active:scale-[.99] transition-transform">Добавить</button>
      <div data-comments-status class="text-[12px] text-mut"></div>
    </div>
  </section>`;
}

async function addComment(button, recId) {
  const block = button && button.closest('[data-comments]');
  const input = block && block.querySelector('[data-comment-text]');
  const status = block && block.querySelector('[data-comments-status]');
  const text = input && input.value != null ? String(input.value).trim() : '';
  if (!text) { if (status) status.innerHTML = esc('Введите комментарий.'); return; }
  if (button.hasAttribute('disabled')) return;
  button.setAttribute('disabled', '');
  if (status) status.innerHTML = esc('Сохранение…');
  try {
    const result = await apiPost(`/api/recordings/${encodeURIComponent(recId)}/comments`, { text });
    const comment = result && result.comment;
    if (comment && _detail && _detail.id === recId) {
      _detail.comments = (Array.isArray(_detail.comments) ? _detail.comments : []).concat([comment]);
      const live = document.querySelector('[data-comments]');
      if (live) live.innerHTML = commentsHtml(_detail).replace(/^<section[^>]*>|<\/section>$/g, '');
    }
  } catch (e) {
    if (e.message === 'unauth') return;
    // Keep the textarea intact so a failed save never loses the reader's text.
    if (status) status.innerHTML = esc(e.messageRu || 'Не удалось сохранить комментарий.');
  } finally {
    const live = document.querySelector('[data-comment-add]');
    if (live) live.removeAttribute('disabled');
  }
}

// ---------- structured summary infographic ----------
function summaryInfographicHtml(r) {
  const d = (r.summary_card_data && typeof r.summary_card_data === 'object' && !Array.isArray(r.summary_card_data))
    ? r.summary_card_data : {};
  if (!Object.keys(d).length) return '';
  const list = (v) => Array.isArray(v) ? v : [];
  const val = (v) => esc(String(v == null ? '' : v));
  const overview = d.overview || '';
  const themes = list(d.themes);
  const facts = list(d.facts);
  const decisions = list(d.decisions).slice(0, 6);
  const risks = list(d.risks).slice(0, 6);
  const tasks = list(d.tasks);
  const taskRows = tasks.map((task) => {
    const completed = !!task.completed;
    const meta = [task.owner, task.due].filter(Boolean).map(val).join(' · ');
    return `<button type="button" role="checkbox" aria-checked="${completed ? 'true' : 'false'}"
      data-task-id="${esc(String(task.id))}" data-rec="${esc(r.id)}"
      class="w-full text-left flex gap-2.5 rounded-xl px-1 py-1 ${completed ? 'line-through text-[#716d78]' : ''}">
      <span aria-hidden="true" class="mt-0.5 w-4 h-4 shrink-0 rounded border-2 border-[#7868e6] text-center text-[11px] leading-[12px]">${completed ? '✓' : ''}</span>
      <span class="min-w-0"><span class="block text-[13px] font-semibold">${val(task.text)}</span>${meta ? `<span class="block text-[10.5px] text-[#716d78] mt-0.5">${meta}</span>` : ''}</span>
    </button>`;
  }).join('');
  const bullets = (items, color) => items.length
    ? `<ul class="space-y-2">${items.map(item => {
        const text = typeof item === 'object' ? (item.text || item.summary || item.task || item.title || '') : item;
        return `<li class="flex gap-2 text-[13.5px] leading-snug"><span class="mt-1.5 w-1.5 h-1.5 shrink-0 rounded-full ${color}"></span><span>${val(text)}</span></li>`;
      }).join('')}</ul>` : '<div class="text-xs text-mut">Нет данных</div>';
  let html = `<section class="mb-4 rounded-3xl overflow-hidden border border-line/60 bg-[#f3f1ec] text-[#29272f] shadow-lg">`;
  html += `<div class="p-5 bg-gradient-to-br from-[#eeebff] to-[#f8f6f0]">
    <div class="text-[10px] tracking-[.18em] font-bold text-[#7265cd] mb-2">PLAUD · КРАТКОЕ РЕЗЮМЕ</div>
    ${overview ? `<p class="text-[19px] leading-snug font-bold">${val(overview)}</p>` : ''}
  </div>`;
  if (themes.length) html += `<div class="px-4 pt-4"><div class="text-[11px] tracking-wide font-bold text-[#77727f] mb-2">ГЛАВНЫЕ ТЕМЫ</div>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">${themes.map(t => {
      const obj = typeof t === 'object' ? t : { title: 'Тема', detail: t };
      const detail = obj.detail || '';
      return `<div class="rounded-2xl bg-white border border-[#e5e0d8] p-3.5"><div class="text-[13px] font-bold text-[#6f61d5] mb-1">${val(obj.title || 'Тема')}</div><div class="text-[13px] leading-snug text-[#55515c]">${val(detail)}</div></div>`;
    }).join('')}</div></div>`;
  if (facts.length) html += `<div class="px-4 pt-4"><div class="text-[11px] tracking-wide font-bold text-[#77727f] mb-2">КЛЮЧЕВЫЕ ФАКТЫ</div>
    <div class="grid grid-cols-2 sm:grid-cols-3 gap-2">${facts.map(f => {
      const obj = typeof f === 'object' ? f : { value: f, label: '' };
      const label = obj.label ? `<div class="text-[10.5px] text-white/60 mt-1">${val(obj.label)}</div>` : '';
      return `<div class="rounded-2xl bg-[#29272f] text-white p-3"><div class="font-bold text-[17px] leading-tight">${val(obj.value || obj.number || '')}</div>${label}</div>`;
    }).join('')}</div></div>`;
  if (decisions.length || risks.length) html += `<div class="grid grid-cols-1 sm:grid-cols-2 gap-2 p-4">
    <div class="rounded-2xl bg-white border border-[#dce9e3] p-3.5"><div class="font-bold text-[13px] text-[#28765f] mb-2">Решения</div>${bullets(decisions, 'bg-[#2d8b6e]')}</div>
    <div class="rounded-2xl bg-white border border-[#eedddd] p-3.5"><div class="font-bold text-[13px] text-[#ad5656] mb-2">Риски</div>${bullets(risks, 'bg-[#bd5d5d]')}</div>
  </div>`;
  if (tasks.length) html += `<div class="mx-4 mb-4 rounded-2xl bg-[#e8e4ff] p-3.5"><div class="font-bold text-[12px] tracking-wide text-[#695acb] mb-2">ЗАДАЧИ</div>
    <div class="space-y-2">${taskRows}</div></div>`;
  if (r.has_summary_card) html += `<a href="/api/recordings/${encodeURIComponent(r.id)}/summary-card.png?v=${encodeURIComponent(r.summary_card_revision || '')}" target="_blank" rel="noopener" class="block mx-4 mb-4 text-center text-[12px] font-semibold text-[#695acb]">Открыть визуальную карточку PNG</a>`;
  return html + '</section>';
}

function asrRouteHtml(r) {
  const route = r.asr_route || {};
  const engine = route.selected_engine || r.engine;
  const reason = route.route_reason;
  if (!engine && !reason) return '';
  return `<div class="mb-3 rounded-xl border border-line/50 bg-panel px-3 py-2.5 text-[11.5px] text-mut">
    <div class="font-semibold text-[#c9c3ff] mb-0.5">Маршрут распознавания</div>
    ${engine ? `<div>Движок: <span class="text-[#e7e7ee]">${esc(String(engine))}</span></div>` : ''}
    ${reason ? `<div>Причина выбора: <span class="text-[#e7e7ee]">${esc(String(reason))}</span></div>` : ''}
  </div>`;
}

// ---------- mind map ----------
// Rendered server-side as a PNG. Shown fit-to-width; tap opens it fullscreen
// where it is displayed at natural size inside a pan/pinch-zoom container.
function mindmapHtml(r) {
  if (!r.has_mindmap) return '';
  const revision = String(r.mindmap_revision || '');
  const src = `/api/recordings/${encodeURIComponent(r.id)}/mindmap.png?v=${encodeURIComponent(revision)}`;
  return `
    <div class="mb-3">
      <div class="text-[13px] font-semibold text-mut mb-2 px-1">Карта записи</div>
      <button data-mm-open="${esc(r.id)}" data-mm-revision="${esc(revision)}" class="block w-full rounded-2xl overflow-hidden border border-line/50 bg-white active:scale-[.99] transition-transform">
        <img src="${src}" alt="Карта записи" loading="lazy"
             onerror="this.closest('.mb-3').remove()"
             class="max-w-full w-auto h-auto max-h-[360px] md:max-h-[480px] object-contain block mx-auto" />
      </button>
      <div class="text-[11.5px] text-mut mt-1.5 px-1">Нажмите, чтобы открыть на весь экран</div>
    </div>`;
}

// Where focus was when the overlay opened, so closing puts the reader back on
// the control they used instead of at the top of the document.
let _mmReturnFocus = null;
// The keyboard trap, kept so closing detaches exactly the listener it attached.
let _mmKeydown = null;
// What #app and <body> looked like before the overlay hid them. Restoring has
// to be exact, not approximate: an attribute that was absent must end up absent
// again rather than present-and-empty, and a page that was already scroll-locked
// (or already inert) must stay that way after the map closes.
let _mmPrev = null;

function openMindmap(id, revision = '') {
  if (document.getElementById('mm-overlay')) return;
  const src = `/api/recordings/${encodeURIComponent(id)}/mindmap.png?v=${encodeURIComponent(revision)}`;
  _mmReturnFocus = document.activeElement || null;
  const ov = document.createElement('div');
  ov.id = 'mm-overlay';
  ov.className = 'fixed inset-0 z-50 bg-black';
  // A real modal to assistive tech: it names itself, announces that the page
  // behind it is unavailable, and (below) hides that page from the reading
  // order for as long as it is up.
  ov.setAttribute('role', 'dialog');
  ov.setAttribute('aria-modal', 'true');
  ov.setAttribute('aria-label', 'Карта записи');
  ov.innerHTML = `
    <div class="absolute inset-0 overflow-auto bg-white md:flex md:items-center md:justify-center" style="padding-top:calc(env(safe-area-inset-top, 0px) + 12px);touch-action:pan-x pan-y pinch-zoom;-webkit-overflow-scrolling:touch;overscroll-behavior:contain">
      <img src="${src}" alt="Карта записи" class="block w-full h-auto md:w-auto md:max-w-[calc(100vw-48px)] md:max-h-[calc(100vh-48px)] md:mx-auto" style="max-width:100%;touch-action:pan-x pan-y pinch-zoom" />
    </div>
    <button data-mm-close aria-label="Закрыть"
      class="absolute z-[60] right-3 w-11 h-11 rounded-full bg-black/75 backdrop-blur flex items-center justify-center shadow-lg"
      style="top:calc(env(safe-area-inset-top, 0px) + 12px)">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>`;
  document.body.appendChild(ov);

  // Take the page behind the overlay out of both the reading order (aria-hidden)
  // and the interaction/tab order (inert), remembering precisely what was there.
  // The attribute is set alongside the property because iOS Safari carried the
  // `inert` attribute before it exposed the IDL property, and this app is
  // installed as a PWA on exactly those devices.
  const appEl = document.getElementById('app');
  _mmPrev = {
    el: appEl,
    hadAriaHidden: !!appEl && appEl.hasAttribute('aria-hidden'),
    ariaHidden: appEl ? appEl.getAttribute('aria-hidden') : null,
    hadInertAttr: !!appEl && appEl.hasAttribute('inert'),
    inertAttr: appEl ? appEl.getAttribute('inert') : null,
    hadInertProp: !!appEl && ('inert' in appEl),
    inertProp: appEl ? appEl.inert : undefined,
    bodyOverflow: document.body.style.overflow,
  };
  if (appEl) {
    appEl.setAttribute('aria-hidden', 'true');
    appEl.setAttribute('inert', '');
    appEl.inert = true;
  }
  document.body.style.overflow = 'hidden';

  // Focus moves into the dialog so a keyboard or screen-reader user lands on
  // the one control it has. preventScroll because the container is a pan/zoom
  // surface: scrolling it to the button would move the map out from under a
  // reader who has not asked for that.
  const focusClose = () => {
    const btn = ov.querySelector('[data-mm-close]');
    if (btn) btn.focus({ preventScroll: true });
  };
  focusClose();

  // A modal owns the keyboard while it is up. The overlay holds exactly one
  // focusable control, so both Tab and Shift+Tab resolve back to it and focus
  // can never walk onto the page we just hid. Escape deliberately does NOT
  // close: dismissal stays as explicit as it is for taps.
  _mmKeydown = (event) => {
    if (event.key !== 'Tab') return;
    event.preventDefault();
    focusClose();
  };
  ov.addEventListener('keydown', _mmKeydown);

  // Closing is deliberately explicit. Taps belong to pan/zoom and must never
  // dismiss the image accidentally; only the fixed X button closes it.
  ov.addEventListener('click', (event) => {
    if (event.target.closest('[data-mm-close]')) closeMindmap();
  });
}

function closeMindmap() {
  const ov = document.getElementById('mm-overlay');
  if (ov) {
    if (_mmKeydown) ov.removeEventListener('keydown', _mmKeydown);
    ov.remove();
  }
  _mmKeydown = null;

  const prev = _mmPrev;
  _mmPrev = null;
  if (prev) {
    const el = prev.el;
    if (el) {
      if (prev.hadAriaHidden) el.setAttribute('aria-hidden', prev.ariaHidden);
      else el.removeAttribute('aria-hidden');
      if (prev.hadInertAttr) el.setAttribute('inert', prev.inertAttr);
      else el.removeAttribute('inert');
      // The property reflects the attribute, so it is restored last.
      if (prev.hadInertProp) el.inert = prev.inertProp;
      else delete el.inert;
    }
    document.body.style.overflow = prev.bodyOverflow;
  }

  // Back to the control that opened the map — unless the view was re-rendered
  // underneath the overlay, in which case that node is gone and focusing it
  // would silently drop focus to the document instead.
  const trigger = _mmReturnFocus;
  _mmReturnFocus = null;
  if (trigger && trigger.isConnected && typeof trigger.focus === 'function') {
    trigger.focus({ preventScroll: true });
  }
}

const DETAIL_TABS = ['summary', 'transcript', 'speakers'];

function summaryStateHtml(r) {
  const state = r && r.summary_state && r.summary_state.state;
  if (!state || state === 'ready') return '';
  const code = String((r.summary_state && r.summary_state.language) || summaryLanguage).toLowerCase();
  const language = code === 'en' ? 'English' : code.toUpperCase();
  const messages = {
    queued: `${language} summary is queued.`, processing: `${language} summary is being generated.`,
    retry_wait: `${language} summary will retry shortly.`, failed: `${language} summary generation failed.`,
    unavailable: `${language} summary service is temporarily unavailable.`, error: `${language} summary could not be requested.`
  };
  const retry = r.summary_state.retryable
    ? `<button type="button" data-summary-retry data-language="${esc(summaryLanguage)}" data-rec="${esc(r.id)}" class="mt-3 min-h-11 rounded-xl border border-line px-4 font-semibold">Retry</button>` : '';
  return `<div class="bg-card border border-line/50 rounded-2xl p-4 text-sm text-mut" role="status">${esc(messages[state] || `${language} summary is pending.`)}${retry}</div>`;
}

async function retrySummary(recId, language) {
  try {
    await apiPost(`/api/recordings/${encodeURIComponent(recId)}/summary/${encodeURIComponent(language)}/retry`, {});
    if (_detail && _detail.id === recId) {
      _detail.summary_state = {language, state:'queued', retryable:false};
      showTab('summary');
    }
  } catch (e) {
    if (_detail && _detail.id === recId) {
      _detail.summary_state = {language, state:'error', retryable:true};
      showTab('summary');
    }
  }
}

function showTab(tab) {
  _detailTab = DETAIL_TABS.indexOf(tab) >= 0 ? tab : 'summary';
  tab = _detailTab;
  const r = _detail;
  if (!r) return;
  // Switching tabs must not leave a snippet playing on behind the reader's back.
  stopSnippet();
  const base = 'flex-1 text-[14px] font-semibold py-2 rounded-lg transition-colors';
  for (const name of DETAIL_TABS) {
    const button = document.getElementById('tab-' + name);
    if (!button) continue;
    button.className = `${base} ${tab === name ? 'bg-card text-white shadow' : 'text-mut'}`;
    // Announce the selection, not just paint it.
    button.setAttribute('aria-selected', tab === name ? 'true' : 'false');
  }
  const body = document.getElementById('detail-body');
  if (!body) return;
  if (tab === 'speakers') {
    body.innerHTML = localized(speakersTabHtml(r));
    if (typeof bindIdentitySelector === 'function') bindIdentitySelector(body, r.id);
    if (typeof identityPeople === 'function' && !_identityPeople) {
      identityPeople().then(() => {
        if (_detail && _detail.id === r.id && _detailTab === 'speakers') showTab('speakers');
      }).catch(() => {});
    }
    return;
  }
  if (tab === 'summary') {
    const summaryBlock = r.summary
      ? `<div class="bg-card border border-line/50 rounded-2xl p-4 text-[15px] leading-relaxed md">${renderMd(r.summary)}</div>`
      : (summaryStateHtml(r) || `<div class="text-mut text-sm bg-card border border-line/50 rounded-2xl p-4">Резюме ещё не готово.</div>`);
    body.innerHTML = localized(`<div class="fade-in">${summaryLanguageControlHtml()}${summaryInfographicHtml(r)}${asrRouteHtml(r)}${summaryBlock}${mindmapHtml(r)}${reprocessHtml(r)}</div>`);
  } else {
    body.innerHTML = `<div class="fade-in">${transcriptTabHtml(r)}${localized(reprocessHtml(r))}</div>`;
  }
}

async function archiveAction(id, action) {
  try { await apiPost(`/api/recordings/${encodeURIComponent(id)}/${action}`, {}); location.hash = action === 'delete' ? '#/archive' : '#/'; }
  catch (_) { alert('Не удалось выполнить действие. Попробуйте позже.'); }
}
function confirmLocalDelete(id) {
  const modal = document.createElement('div'); modal.setAttribute('role','dialog'); modal.setAttribute('aria-modal','true');
  modal.className = 'fixed inset-0 z-50 flex items-end bg-black/70 p-4';
  modal.innerHTML = `<div class="w-full rounded-2xl bg-card p-5"><h2 class="font-bold text-lg">Удалить запись?</h2><p class="text-mut text-sm mt-2">Будут удалены только локальные данные и аудио. Исходная запись PLAUD не изменится.</p><div class="flex gap-2 mt-5"><button data-cancel class="flex-1 rounded-xl border border-line py-3">Отмена</button><button data-confirm class="flex-1 rounded-xl bg-red-600 py-3 font-semibold">Да, удалить</button></div></div>`;
  document.body.appendChild(modal); modal.querySelector('[data-cancel]').onclick=()=>modal.remove(); modal.querySelector('[data-confirm]').onclick=()=>{modal.remove();archiveAction(id,'delete');};
}
function archiveControls(r) {
  return r.archived
    ? `<section class="mt-3 rounded-2xl border border-line/60 bg-panel p-3"><p class="mb-2 text-sm font-semibold text-mut">Запись в архиве</p><div class="grid gap-2"><button data-archive-action="restore" data-rec="${esc(r.id)}" class="rounded-xl bg-accent py-3 font-semibold">Вернуть</button><button data-delete-local data-rec="${esc(r.id)}" class="rounded-xl border border-red-500/60 py-3 text-red-300 font-semibold">Удалить</button></div></section>`
    : `<button data-archive-action="archive" data-rec="${esc(r.id)}" class="mt-3 w-full rounded-xl border border-line bg-panel py-3 font-semibold active:bg-white/5">Заархивировать запись</button>`;
}

function publicShareControlsHtml(r) {
  if (!r || !r.public_share_enabled) return '';
  const active = Array.isArray(r.public_shares) ? r.public_shares : [];
  const activeHtml = active.length
    ? `<div class="mt-3 border-t border-line/50 pt-2 text-[12px] text-mut">Активные ссылки: ${active.length}` +
      active.map((share) =>
        `<button type="button" data-public-revoke="${esc(share.share_id)}" class="ml-2 min-h-11 text-red-300">Отозвать</button>`,
      ).join('') + '</div>'
    : '';
  const option = (id, label, checked, disabled = false) =>
    `<label class="flex min-h-11 items-center gap-2 text-[12px] ${disabled ? 'text-mut' : 'text-white'}">` +
    `<input id="public-share-${id}" type="checkbox" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}>${label}</label>`;
  return `<section id="public-share-controls" class="mt-3 rounded-2xl border border-line/50 bg-card p-3">
    <h2 class="text-[14px] font-semibold">Публичная ссылка</h2>
    <div class="mt-2 grid grid-cols-2 gap-x-2">
      ${option('metadata', 'Название и дата', true)}
      ${option('summary', 'Резюме', Boolean(r.summary), !r.summary)}
      ${option('transcript', 'Транскрипт', Boolean(r.transcript), !r.transcript)}
      ${option('mindmap', 'Карта', Boolean(r.has_mindmap), !r.has_mindmap)}
      ${option('images', 'PNG-карточка', Boolean(r.has_summary_card), !r.has_summary_card)}
      ${option('audio', 'Аудио', Boolean(r.has_audio), !r.has_audio)}
    </div>
    <div class="mt-3 flex items-center gap-2">
      <select id="public-share-ttl" aria-label="Срок действия" class="min-h-11 rounded-xl border border-line/60 bg-ink px-2 text-[13px] text-white">
        <option value="1h">1 час</option>
        <option value="24h" selected>24 часа</option>
        <option value="7d">7 дней</option>
      </select>
      <button type="button" data-public-share="${esc(r.id)}" class="min-h-11 flex-1 rounded-xl bg-accent px-3 text-[14px] font-semibold text-white disabled:opacity-50">
        Создать и скопировать
      </button>
    </div>
    <div id="public-share-status" role="status" aria-live="polite" class="mt-2 min-h-[18px] text-[11.5px] text-mut">Ссылка откроет только отмеченные материалы.</div>
    ${activeHtml}
  </section>`;
}

function refreshPublicShareControls(message) {
  const slot = document.getElementById('public-share-slot');
  if (!_detail || !slot) return;
  slot.innerHTML = publicShareControlsHtml(_detail);
  const status = document.getElementById('public-share-status');
  if (status && message) status.innerHTML = esc(message);
}

const _publicSharePending = new Set();

async function copyPublicShareUrl(url) {
  if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
    return legacyCopyText(url);
  }
  try { await navigator.clipboard.writeText(url); return true; }
  catch (_) { return false; }
}

async function createPublicShare(id, button) {
  if (_publicSharePending.has(id)) return;
  _publicSharePending.add(id);
  const status = document.getElementById('public-share-status');
  if (status) status.innerHTML = 'Экспортирую выбранные материалы…';
  if (button) button.disabled = true;
  const names = ['metadata', 'summary', 'transcript', 'mindmap', 'images', 'audio'];
  const content = {};
  for (const name of names) {
    const field = document.getElementById('public-share-' + name);
    content[name] = Boolean(field && field.checked && !field.disabled);
  }
  const ttlField = document.getElementById('public-share-ttl');
  try {
    const result = await apiPost(
      '/api/recordings/' + encodeURIComponent(id) + '/public-shares',
      { ttl: ttlField ? ttlField.value : '24h', content },
    );
    if (!(await copyPublicShareUrl(result.url))) {
      let cleaned = false;
      try { cleaned = Boolean((await apiDelete('/api/public-shares/' + encodeURIComponent(result.share_id))).revoked); }
      catch (_) {}
      if (_detail && _detail.id === id && !cleaned) {
        _detail.public_shares = Array.isArray(_detail.public_shares) ? _detail.public_shares : [];
        _detail.public_shares.unshift(result);
      }
      refreshPublicShareControls(cleaned
        ? 'Не удалось скопировать. Экспорт удалён.'
        : 'Не удалось скопировать. Отзовите созданную ссылку.');
      return;
    }
    if (_detail && _detail.id === id) {
      _detail.public_shares = Array.isArray(_detail.public_shares) ? _detail.public_shares : [];
      _detail.public_shares.unshift(result);
      refreshPublicShareControls(`Ссылка скопирована. Действует до ${result.expires_at}.`);
    }
  } catch (error) {
    const live = document.getElementById('public-share-status');
    if (live && error.message !== 'unauth') live.innerHTML = esc(error.messageRu || 'Не удалось создать ссылку.');
  } finally {
    _publicSharePending.delete(id);
    if (button && button.isConnected) button.disabled = false;
  }
}

async function revokePublicShare(shareId, button) {
  if (button) button.disabled = true;
  try {
    const result = await apiDelete('/api/public-shares/' + encodeURIComponent(shareId));
    if (_detail) {
      _detail.public_shares = (Array.isArray(_detail.public_shares) ? _detail.public_shares : [])
        .filter((share) => share.share_id !== shareId);
      refreshPublicShareControls(result.revoked ? 'Ссылка отозвана.' : 'Ссылка уже неактивна.');
    }
  } catch (error) {
    const status = document.getElementById('public-share-status');
    if (status && error.message !== 'unauth') status.innerHTML = esc(error.messageRu || 'Не удалось отозвать ссылку.');
  } finally {
    if (button && button.isConnected) button.disabled = false;
  }
}

async function renderArchive(gen, append=false) {
  if (!append) {
    _archiveCursor = null;
    _archiveRows = [];
    app.innerHTML = localized(header('Архив',{back:'#/'}) + skeletonFeed());
  }
  let payload;
  const cursorPart = append && _archiveCursor ? `&cursor=${encodeURIComponent(_archiveCursor)}` : '';
  try {
    payload = await api('/api/recordings?archived=1&page=cursor' + cursorPart + languageQuery());
  } catch (_) {
    if (!append) app.innerHTML = localized(header('Архив',{back:'#/'})+errBox('Не удалось загрузить архив'));
    return;
  }
  if (gen !== _navGen) return;
  if (!Array.isArray(payload)) setDisplayTimezone(payload && payload.display_timezone);
  const rows = Array.isArray(payload) ? payload : (payload.items || []);
  _archiveRows = append ? [..._archiveRows, ...rows.filter(r=>!_archiveRows.some(x=>x.id===r.id))] : rows;
  _archiveCursor = Array.isArray(payload) ? null : (payload.next_cursor || null);
  const more = _archiveCursor ? `<button type="button" data-archive-more class="w-full min-h-12 rounded-xl border border-line/60 bg-panel font-semibold">Загрузить ещё</button>` : '';
  const archiveBody = !_archiveRows.length
    ? emptyBox('🗄️','Архив пуст','Заархивированные записи появятся здесь.')
    : `<div class="px-4 pt-3 pb-24">${_archiveRows.map(r=>`<button data-rec-id="${esc(r.id)}" class="w-full text-left block bg-card rounded-2xl px-4 py-3.5 mb-2.5 border border-line/50"><h3 class="font-semibold">${esc(r.title||r.name)}</h3><p class="text-mut text-sm mt-1">${esc(r.summary||'')}</p></button>`).join('')}${more}</div>`;
  app.innerHTML = localizedPreserving(header('Архив',{back:'#/'}) + archiveBody,
    _archiveRows.flatMap(r => [r.title, r.name, r.summary]));
}

async function renderDetail(id, gen) {
  const at = gen == null ? _navGen : gen;
  app.innerHTML = header('', { back: '#/' }) + skeletonFeed();
  let r;
  try { r = await api('/api/recordings/' + encodeURIComponent(id) + (summaryLanguage === 'ru' ? '' : `?lang=${encodeURIComponent(summaryLanguage)}`)); }
  catch (e) {
    if (e.message !== 'unauth' && !stale(at)) {
      app.innerHTML = header('', { back:'#/' }) + errBox('Запись не найдена');
    }
    asrPoller.stop();   // an error box has nothing to refresh
    return null;
  }
  if (stale(at)) return null;   // the reader navigated away while this loaded

  setDisplayTimezone(r && r.display_timezone);
  _detail = r;
  _detailTab = (r.summary_data || r.summary || r.has_mindmap || (r.summary_state && r.summary_state.language !== 'ru')) ? 'summary' : 'transcript';

  const d = recordingDate(r);
  const dateLine = compactDateTime(d);
  const meta = [dateLine, r.duration_ms ? fmtDur(r.duration_ms) : '', r.engine ? r.engine : '']
    .filter(Boolean).join('  ·  ');

  // Keep playback near the top of the recording. Download is an infrequent
  // action and is rendered separately at the absolute bottom of the detail.
  const audioBlock = r.has_audio
    ? `<div class="-mx-4 px-4 pt-2 pb-2.5 border-b border-line/40">
         <audio id="rec-audio" controls preload="metadata" src="/audio/${encodeURIComponent(r.id)}"></audio>
       </div>`
    : '';
  const audioDownload = r.has_audio
    ? `<a href="/audio/${encodeURIComponent(r.id)}?download=1" download="${esc(r.id)}.mp3"
          class="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-line/60 bg-card px-3 py-2.5 text-[14px] font-semibold text-white active:scale-[.99]"
          aria-label="Скачать аудиозапись">
         <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M5 21h14"></path></svg>
         Скачать аудио
       </a>`
    : '';

  const tabs = `
    <div role="tablist" aria-label="Разделы записи"
         class="flex gap-1 bg-panel border border-line/50 rounded-xl p-1 mt-4 mb-4">
      <button id="tab-summary" role="tab" onclick="showTab('summary')" class="flex-1 text-[14px] font-semibold py-2 rounded-lg transition-colors">Резюме</button>
      <button id="tab-transcript" role="tab" onclick="showTab('transcript')" class="flex-1 text-[14px] font-semibold py-2 rounded-lg transition-colors">Транскрипт</button>
      <button id="tab-speakers" role="tab" onclick="showTab('speakers')" class="flex-1 text-[14px] font-semibold py-2 rounded-lg transition-colors">Спикеры</button>
    </div>`;

  app.innerHTML = localized(header('', { back: '#/' }) + `
    <div class="px-4 pt-1 pb-28 fade-in">
      <h1 class="text-[22px] font-bold leading-tight mt-1">${esc(r.title || r.name)}</h1>
      <div class="text-[12.5px] text-mut mt-1.5">${esc(meta)}</div>
      <div class="mt-2 empty:hidden">${codeCopyButton(r)}</div>
      <div class="mt-2.5 empty:hidden" data-asr-id="${esc(r.id)}">${asrProgressHtml(r.asr_processing)}</div>
      ${audioBlock}
      ${tabs}
      <div id="detail-body"></div>
      ${commentsHtml(r)}
      <section data-detail-actions aria-label="Действия с записью"
               class="mt-8 border-t border-line/40 pt-5">
        <div class="mb-1 text-[12px] font-semibold uppercase tracking-wide text-mut">Действия</div>
        <div id="public-share-slot">${publicShareControlsHtml(r)}</div>
        ${archiveControls(r)}
        ${audioDownload}
      </section>
    </div>`);
  showTab(_detailTab);
  asrPoller.note(r);
  return r;
}

// A silent detail refresh. The badge is patched in place; #detail-body is
// rebuilt only when its content actually changed. The <audio> element and the
// tab the reader picked live outside the body and are never re-created, so
// playback and position survive every tick.
async function refreshDetail(id, gen) {
  let r;
  try { r = await api('/api/recordings/' + encodeURIComponent(id) + (summaryLanguage === 'ru' ? '' : `?lang=${encodeURIComponent(summaryLanguage)}`)); }
  catch (e) { return null; }   // transient: retry on the next tick
  // Dropped if the reader navigated away, or opened a different recording,
  // while this was in flight — a late response must never repaint a new view.
  if (stale(gen) || _route.kind !== 'detail' || _route.id !== id) return null;

  setDisplayTimezone(r && r.display_timezone);
  const before = _detail;
  _detail = r;
  patchAsrSlots(asrSlotUpdates(r));
  const commentsChanged = JSON.stringify((before && before.comments) || [])
    !== JSON.stringify(r.comments || []);
  const tasksChanged = JSON.stringify((before && before.tasks) || [])
    !== JSON.stringify(r.tasks || []);
  const publicSharesChanged = JSON.stringify((before && before.public_shares) || [])
    !== JSON.stringify(r.public_shares || []);
  if (detailNeedsRerender(before, r) || tasksChanged) showTab(_detailTab);
  if (publicSharesChanged) refreshPublicShareControls();
  if (commentsChanged) {
    const live = document.querySelector('[data-comments]');
    if (live) live.innerHTML = commentsHtml(r).replace(/^<section[^>]*>|<\/section>$/g, '');
  }
  return r;
}

// ---------- search ----------
let searchTimer = null;
let semanticSearchTimer = null;
let searchRequest = 0;
let semanticRenderedRequest = 0;
function renderSearch() {
  clearTimeout(searchTimer);
  clearTimeout(semanticSearchTimer);
  searchRequest++;
  semanticRenderedRequest = 0;
  app.innerHTML = localized(header('Поиск', { back: '#/' }) + `
    <div class="px-4 pt-3">
      <div class="flex items-center gap-2 bg-card border border-line/60 rounded-2xl px-3.5 h-11">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#8e8e98" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/></svg>
        <input id="q" autocomplete="off" autocapitalize="off" placeholder="Поиск по записям"
          class="flex-1 bg-transparent outline-none text-[15px] placeholder-mut" />
      </div>
    </div>
    <div id="results" class="px-4 pt-4 pb-24"></div>`);
  const input = document.getElementById('q');
  input.focus();
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    clearTimeout(semanticSearchTimer);
    const v = input.value.trim();
    const request = ++searchRequest;
    semanticRenderedRequest = 0;
    if (!v) {
      document.getElementById('results').innerHTML = '';
      return;
    }
    searchTimer = setTimeout(() => doSearch(v, request, false), 220);
    if (v.length >= 3) {
      semanticSearchTimer = setTimeout(() => doSearch(v, request, true), 450);
    }
  });
}

async function doSearch(q, request, semantic = false) {
  const box = document.getElementById('results');
  if (!box) return;
  if (!q) { box.innerHTML = ''; return; }
  if (!semantic) box.innerHTML = `<div class="text-center text-mut text-sm pt-8">Поиск…</div>`;
  let rows;
  const suffix = semantic ? '&semantic=true' : '';
  try { rows = await api('/api/search?q=' + encodeURIComponent(q) + suffix); }
  catch (e) {
    if (e.message === 'unauth' || request !== searchRequest) return;
    if (semantic) return;
    box.innerHTML = errBox('Ошибка поиска');
    return;
  }
  // guard: input may have changed
  const cur = document.getElementById('q');
  if (request !== searchRequest || (cur && cur.value.trim() !== q)) return;
  if (!semantic && semanticRenderedRequest === request) return;
  if (semantic) semanticRenderedRequest = request;
  if (!rows.length) { box.innerHTML = emptyBox('🔍','Ничего не найдено','Попробуйте другой запрос.'); return; }
  let html = `<div class="text-[13px] text-mut mb-2 px-1">${rows.length} ${plural(rows.length,'результат','результата','результатов')}</div>`;
  for (const r of rows) {
    const snip = highlightSnippet(r.snippet);
    const semanticLabel = r.label === 'По смыслу'
      ? `<span class="inline-flex mt-1 rounded-full border border-line px-2 py-0.5 text-[11px] text-mut">По смыслу</span>`
      : '';
    html += `
      <button data-rec-id="${esc(r.id)}" class="w-full text-left block bg-card rounded-2xl px-4 py-3.5 mb-2.5 border border-line/50 active:scale-[.985] transition-transform">
        <h3 class="font-semibold text-[15px] truncate">${esc(r.title || r.name)}</h3>
        ${semanticLabel}
        <p class="text-[13px] text-mut mt-1 leading-snug line-clamp-2">${snip}</p>
      </button>`;
  }
  box.innerHTML = `<div class="fade-in">${html}</div>`;
}
function plural(n, a, b, c) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return a;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return b;
  return c;
}
function highlightSnippet(s) {
  // server wraps hits in 【 】 → convert to <mark>
  return esc(s || '').replace(/【/g, '<mark>').replace(/】/g, '</mark>');
}

// ---------- router ----------
function route() {
  const h = location.hash || '#/';
  const gen = ++_navGen;   // invalidates every request the old view had open
  window.scrollTo(0, 0);
  if (h.startsWith('#/r/')) {
    _route = { kind: 'detail', id: h.slice(4), gen };
    return renderDetail(_route.id, gen);
  }
  if (h === '#/archive') {
    _route = { kind: 'archive', id: null, gen };
    asrPoller.stop();
    return renderArchive(gen);
  }
  if (h === '#/search') {
    _route = { kind: 'search', id: null, gen };
    asrPoller.stop();   // nothing on the search screen reports progress
    return renderSearch();
  }
  _route = { kind: 'feed', id: null, gen };
  return renderFeed({ gen });
}
window.addEventListener('hashchange', route);
route();
