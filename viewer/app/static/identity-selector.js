/* Manual identity selector. No voice samples or enrollment in this phase. */
'use strict';

let _identityPeople = null;

async function identityPeople() {
  if (_identityPeople) return _identityPeople;
  const response = await fetch('/api/people', { credentials: 'same-origin' });
  if (!response.ok) throw new Error('people unavailable');
  const body = await response.json();
  _identityPeople = Array.isArray(body.people) ? body.people : [];
  return _identityPeople;
}

function identitySelectorHtml(s) {
  const current = (s.identity && s.identity.person_id) || '';
  const known = (_identityPeople || []).map((p) =>
    `<option value="${esc(p.person_id)}"${p.person_id === current ? ' selected' : ''}>${esc(p.display_name)}</option>`).join('');
  return `<div data-identity-selector class="mt-2.5 border-t border-line/40 pt-2.5">
    <label class="block text-[11.5px] text-mut mb-1" for="identity-${esc(s.speaker_id)}">Известный человек</label>
    <select id="identity-${esc(s.speaker_id)}" data-identity-person="${esc(s.speaker_id)}"
      class="w-full min-h-11 bg-panel border border-line/60 rounded-xl px-3 text-[14px] text-white">
      <option value="">Не выбран</option>${known}
    </select>
    <div class="flex gap-2 mt-2">
      <button data-identity-new="${esc(s.speaker_id)}" class="min-h-11 flex-1 rounded-xl border border-line/60 text-[12px]">Новый человек</button>
      <button data-identity-unknown="${esc(s.speaker_id)}" class="min-h-11 flex-1 rounded-xl border border-line/60 text-[12px]">Неизвестный</button>
      <button data-identity-undo="${esc(s.speaker_id)}" class="min-h-11 flex-1 rounded-xl border border-line/60 text-[12px]">Отменить</button>
    </div>
    <div data-identity-status="${esc(s.speaker_id)}" role="status" aria-live="polite" class="text-[11.5px] text-mut mt-1"></div>
  </div>`;
}

async function identityMutation(recId, speakerId, body, undo) {
  const path = `/api/recordings/${encodeURIComponent(recId)}/speakers/${encodeURIComponent(speakerId)}/identity${undo ? '/undo' : ''}`;
  const status = document.querySelector(`[data-identity-status="${speakerId}"]`);
  if (status) status.innerHTML = 'Сохранение…';
  try {
    await apiPost(path, body);
    if (status) status.innerHTML = 'Сохранено.';
  } catch (error) {
    if (status) status.innerHTML = esc(error.messageRu || 'Не удалось сохранить.');
  }
}

function bindIdentitySelector(root, recId) {
  root.addEventListener('change', (event) => {
    const field = event.target && event.target.closest && event.target.closest('[data-identity-person]');
    if (!field || !field.value) return;
    identityMutation(recId, field.getAttribute('data-identity-person'), { person_id: field.value }, false);
  });
  root.addEventListener('click', async (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== 'function') return;
    const unknown = target.closest('[data-identity-unknown]');
    if (unknown) {
      identityMutation(recId, unknown.getAttribute('data-identity-unknown'), { unknown: true }, false);
      return;
    }
    const undo = target.closest('[data-identity-undo]');
    if (undo) {
      identityMutation(recId, undo.getAttribute('data-identity-undo'), {}, true);
      return;
    }
    const add = target.closest('[data-identity-new]');
    if (!add) return;
    const name = typeof prompt === 'function' ? prompt('Имя человека') : '';
    if (!name || !name.trim()) return;
    const thisIsMe = typeof confirm === 'function'
      ? confirm('Это Вы? Подтвердите только для своего голоса.') : false;
    try {
      const created = await apiPost('/api/people', {
        display_name: name.trim(), ...(thisIsMe ? { this_is_me: true } : {}),
      });
      _identityPeople = (_identityPeople || []).concat([created.person]);
      await identityMutation(recId, add.getAttribute('data-identity-new'),
        { person_id: created.person.person_id }, false);
      if (_detail && _detail.id === recId) showTab('speakers');
    } catch (_) {}
  });
}
