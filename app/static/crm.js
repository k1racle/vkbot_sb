/* Client directory and explicit, confirmed broadcasts. No send on page load. */
(() => {
  const { api, escape: esc } = Admin;
  const $ = (id) => document.getElementById(id);
  const toast = (text) => Admin.toast(text);
  const statusNames = { draft: 'Черновик', queued: 'В очереди', running: 'Отправляется', paused: 'На паузе', cancelled: 'Отменена', completed: 'Завершена', pending: 'Ожидает', sending: 'Отправляется', sent: 'Доставлено VK', skipped: 'Пропущено', failed: 'Ошибка', done: 'Обработано' };
  const badge = (s) => `<span class="crm-badge crm-status-${esc(s)}">${esc(statusNames[s] || s)}</span>`;
  const date = (value) => value ? new Date(value.replace(' ', 'T') + (/[Z+]\d*:?\d*$/.test(value) ? '' : 'Z')).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—';
  const name = (c) => [c.first_name, c.last_name].filter(Boolean).join(' ') || `Клиент ${c.user_id}`;
  const avatar = (c) => c.photo_url ? `<img class="crm-avatar" src="${esc(c.photo_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="crm-avatar crm-avatar-empty" aria-hidden="true">${esc((c.first_name || '?').slice(0, 1))}</span>`;
  let selected;
  try { selected = new Set(JSON.parse(sessionStorage.getItem('broadcast_selection') || '[]').filter((id) => Number.isInteger(id) && id > 0)); } catch { selected = new Set(); }
  const remember = () => { try { sessionStorage.setItem('broadcast_selection', JSON.stringify([...selected])); } catch { /* Private browsing can disable storage. */ } };
  const request = async (button, fn) => {
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try { return await fn(); } catch (e) { toast(e.message); } finally { if (button) button.disabled = false; }
  };
  document.querySelectorAll('[data-close-dialog]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));

  if ($('crm-rows')) {
    let page = 1, pages = 1, rows = [], sequence = 0, pending = 0;
    function selection() {
      $('crm-mail-selected').textContent = `Рассылка выбранным (${selected.size})`;
      $('crm-mail-selected').disabled = !selected.size;
      $('crm-clear').hidden = !selected.size;
      const checked = rows.filter((c) => selected.has(c.user_id)).length;
      $('crm-select-page').checked = rows.length > 0 && checked === rows.length;
      $('crm-select-page').indeterminate = checked > 0 && checked < rows.length;
      document.querySelectorAll('[data-select-client]').forEach((b) => { b.checked = selected.has(Number(b.dataset.selectClient)); });
      remember();
    }
    async function load() {
      const seq = ++sequence;
      const data = await api(`/clients?${new URLSearchParams({ q: $('crm-query').value, page, contacted: $('crm-filter').value || 'false' })}`);
      if (seq !== sequence) return;
      rows = data.items; pages = data.pages; pending = data.profiles_pending;
      $('crm-total').textContent = data.total;
      $('crm-page').textContent = `Страница ${data.page} из ${pages}`;
      $('crm-prev').disabled = page <= 1; $('crm-next').disabled = page >= pages;
      $('crm-note').textContent = pending ? `Обновляются профили: ${pending}. Данные появятся автоматически.` : 'Выберите клиентов галочками для отдельной рассылки.';
      $('crm-rows').innerHTML = rows.map((c) => `<tr><td><input type="checkbox" data-select-client="${c.user_id}" aria-label="Выбрать ${esc(name(c))}"></td><td><div class="crm-person">${avatar(c)}<div><strong>${esc(name(c))}</strong><a href="${esc(c.vk_url)}" target="_blank" rel="noopener noreferrer">id${c.user_id} ↗</a></div></div></td><td>${esc(c.phone || 'Не доступен')}<small class="muted">${c.phone ? c.phone_source === 'dialog' ? 'Из диалога' : 'Из VK' : 'VK не передал номер'}</small></td><td>${c.bot_contacted_at ? esc(date(c.bot_contacted_at)) : '<span class="muted">Нет подтверждённой отправки</span>'}</td><td>${c.unsubscribed ? '<span class="crm-badge crm-status-cancelled">Отписался</span>' : c.deactivated ? '<span class="crm-badge">Профиль недоступен</span>' : c.messages_allowed === false ? '<span class="crm-badge">Сообщения запрещены</span>' : '<span class="crm-badge crm-status-pending">Проверим перед отправкой</span>'}</td><td><button class="btn secondary small" data-client="${c.user_id}">Карточка</button></td></tr>`).join('') || '<tr><td class="empty" colspan="6">Клиенты не найдены. Попробуйте другой поиск или дождитесь новых обращений к боту.</td></tr>';
      selection();
    }
    async function detail(id) {
      const d = await api(`/clients/${id}`), c = d.client;
      $('crm-detail-body').innerHTML = `<div class="crm-person crm-person-large">${avatar(c)}<div><h3>${esc(name(c))}</h3><a href="${esc(c.vk_url)}" target="_blank" rel="noopener noreferrer">Открыть VK ↗</a></div></div><dl class="crm-details"><dt>Телефон</dt><dd>${esc(c.phone || 'VK не передал номер')}</dd><dt>Последнее сообщение бота</dt><dd>${esc(date(c.bot_contacted_at))}</dd><dt>Данные профиля обновлены</dt><dd>${esc(date(c.profile_updated_at))}</dd><dt>Менеджер</dt><dd>${d.assigned_operator_id ? esc(d.assigned_operator_id) : d.handoff ? 'Ожидает менеджера' : 'Не назначен'}</dd><dt>Рассылки</dt><dd>${c.unsubscribed ? 'Клиент отписался' : 'Разрешение VK проверяется перед отправкой'}</dd></dl>${c.profile_error ? `<p class="crm-error">${esc(c.profile_error)}</p>` : ''}<div class="crm-actions"><a class="btn secondary small" href="/admin?section=dialogs">Открыть обращения</a>${!c.unsubscribed ? `<button class="btn secondary small" id="crm-unsubscribe" data-id="${c.user_id}">Исключить из рассылок</button>` : ''}</div><h3>Последние входящие сообщения</h3><div class="crm-events">${d.events.map((e) => `<article><small class="muted">${esc(date(e.date))} · ${esc(e.status)}</small><p>${esc(e.text || 'Служебное событие')}</p>${e.error ? `<small class="crm-error">${esc(e.error)}</small>` : ''}</article>`).join('') || '<p class="hint">Сохранённых сообщений пока нет.</p>'}</div><h3>Участие в рассылках</h3>${d.deliveries.map((r) => `<p>${badge(r.status)} ${esc(date(r.date))} <span class="hint">${esc(r.error)}</span></p>`).join('') || '<p class="hint">Ещё не участвовал.</p>'}`;
      if (!$('crm-detail').open) $('crm-detail').showModal();
    }
    $('crm-detail-body').addEventListener('click', (e) => {
      const b = e.target.closest('#crm-unsubscribe');
      if (b && confirm('Исключить клиента из будущих рассылок? Обычные ответы бота сохранятся.')) request(b, async () => { await api(`/clients/${b.dataset.id}/unsubscribe`, 'POST'); await detail(b.dataset.id); await load(); });
    });
    $('crm-rows').addEventListener('click', (e) => { const b = e.target.closest('[data-client]'); if (b) request(b, () => detail(b.dataset.client)); });
    $('crm-rows').addEventListener('change', (e) => { if (!e.target.matches('[data-select-client]')) return; const id = Number(e.target.dataset.selectClient); e.target.checked ? selected.add(id) : selected.delete(id); selection(); });
    $('crm-select-page').addEventListener('change', (e) => { rows.forEach((c) => e.target.checked ? selected.add(c.user_id) : selected.delete(c.user_id)); selection(); });
    $('crm-clear').addEventListener('click', () => { selected.clear(); selection(); });
    $('crm-mail-selected').addEventListener('click', () => { remember(); location.href = '/admin?section=broadcasts&audience=selected'; });
    $('crm-search').addEventListener('submit', (e) => { e.preventDefault(); page = 1; request(e.submitter, load); });
    $('crm-prev').addEventListener('click', () => { page--; request(null, load); });
    $('crm-next').addEventListener('click', () => { page++; request(null, load); });
    $('crm-refresh').addEventListener('click', (e) => request(e.currentTarget, async () => { const r = await api('/clients/refresh', 'POST'); toast(`Поставлено на обновление: ${r.queued}`); await load(); }));
    setInterval(() => { if (pending && !document.hidden && !$('crm-detail').open) request(null, load); }, 5000);
    request(null, load);
  }

  if ($('broadcast-form')) {
    let mediaId = '', uploading = false, eligible = 0, jobs = [], draft = null, busy = false, logId = '', logPage = 1;
    if (new URLSearchParams(location.search).get('audience') === 'selected') $('broadcast-audience').value = 'selected';
    function audienceNote() {
      $('broadcast-audience-note').textContent = $('broadcast-audience').value === 'selected' ? `Выбрано в списке клиентов: ${selected.size}. Неподходящие получатели будут исключены.` : `Сейчас подходит: ${eligible}. Итоговый список увидите перед запуском.`;
    }
    $('broadcast-audience').addEventListener('change', audienceNote);
    function length() { $('broadcast-length').textContent = `${$('broadcast-message').value.length} / 4000`; }
    $('broadcast-message').addEventListener('input', length);
    document.querySelectorAll('[data-variable]').forEach((b) => b.addEventListener('click', () => { const t = $('broadcast-message'), value = `{${b.dataset.variable}}`; if (t.value.length + value.length > 4000) return; t.setRangeText(value, t.selectionStart, t.selectionEnd, 'end'); t.focus(); length(); }));
    $('broadcast-file').addEventListener('change', async () => {
      const file = $('broadcast-file').files[0]; mediaId = '';
      $('broadcast-file-clear').hidden = !file;
      if (!file) { $('broadcast-file-note').textContent = 'Без вложения'; return; }
      if (!file.size || file.size > 50 * 1024 * 1024) { $('broadcast-file').value = ''; $('broadcast-file-note').textContent = 'Выберите непустой файл до 50 МБ.'; return; }
      uploading = true; $('broadcast-prepare').disabled = true; $('broadcast-file').disabled = true; $('broadcast-file-clear').disabled = true; $('broadcast-file-note').textContent = 'Загружаем файл…';
      try { const form = new FormData(); form.append('file', file); const r = await api('/media', 'POST', form); mediaId = r.id; $('broadcast-file-note').textContent = `Прикреплён: ${r.filename}`; }
      catch (e) { $('broadcast-file-note').textContent = e.message; $('broadcast-file').value = ''; toast(e.message); }
      finally { uploading = false; $('broadcast-prepare').disabled = false; $('broadcast-file').disabled = false; $('broadcast-file-clear').disabled = false; }
    });
    $('broadcast-file-clear').addEventListener('click', () => { mediaId = ''; $('broadcast-file').value = ''; $('broadcast-file-note').textContent = 'Без вложения'; $('broadcast-file-clear').hidden = true; });
    async function loadJobs() {
      const d = await api('/broadcasts'); jobs = d.items; eligible = d.eligible; audienceNote();
      $('broadcast-jobs').innerHTML = jobs.map((j) => {
        const c = j.counts, done = (c.sent || 0) + (c.failed || 0) + (c.skipped || 0) + (c.cancelled || 0);
        return `<article class="crm-job"><div class="crm-job-top"><div><h3>${esc(j.title)}</h3><span class="hint">${esc(date(j.created_at))} · ${j.total} получателей</span></div>${badge(j.status)}</div><progress max="${j.total || 1}" value="${done}" aria-label="Обработано получателей"></progress><div class="crm-job-stats"><span>Отправлено <b>${c.sent || 0}</b></span><span>Пропущено <b>${c.skipped || 0}</b></span><span>Ошибки <b>${c.failed || 0}</b></span><span>Ожидает <b>${(c.pending || 0) + (c.sending || 0)}</b></span></div>${j.error ? `<p class="crm-error">${esc(j.error)}</p>` : ''}<div class="crm-actions"><button class="btn secondary small" data-job="${j.id}" data-action="log">Журнал</button>${j.status === 'draft' ? `<button class="btn small" data-job="${j.id}" data-action="preview">Проверить и запустить</button>` : ''}${['queued', 'running'].includes(j.status) ? `<button class="btn secondary small" data-job="${j.id}" data-action="pause">Пауза</button>` : ''}${j.status === 'paused' ? `<button class="btn small" data-job="${j.id}" data-action="resume">Продолжить</button>` : ''}${['draft', 'queued', 'running', 'paused'].includes(j.status) ? `<button class="btn secondary small" data-job="${j.id}" data-action="cancel">Отменить</button>` : ''}</div></article>`;
      }).join('') || '<div class="empty">Рассылок ещё нет. Начните с текста сообщения выше — без подтверждения ничего не отправится.</div>';
    }
    function preview(job) {
      draft = job; $('broadcast-preview-count').textContent = `«${job.title}» — ${job.total} получателей`;
      $('broadcast-preview-text').textContent = job.preview || `${job.message}\n\nЧтобы отказаться от рассылок, напишите «Стоп».`;
      $('broadcast-preview-sample').textContent = job.sample ? `Пример для ${name(job.sample[0])}. Среди получателей: ${job.sample.map(name).join(', ')}.` : 'Переменные заменятся данными каждого получателя. Проверьте текст тестовой отправкой.';
      $('broadcast-preview-file').textContent = job.filename ? `Вложение: ${job.filename}` : 'Без вложения';
      $('broadcast-consent').checked = false; $('broadcast-start').disabled = true; $('broadcast-launch-result').textContent = ''; $('broadcast-test-result').textContent = 'Тест не запускает рассылку. Получатель должен разрешить сообщения сообщества.';
      if (!$('broadcast-preview').open) $('broadcast-preview').showModal();
    }
    $('broadcast-form').addEventListener('submit', (e) => {
      e.preventDefault(); if (uploading) return;
      request($('broadcast-prepare'), async () => {
        const job = await api('/broadcasts', 'POST', { title: $('broadcast-title').value, message: $('broadcast-message').value, media_id: mediaId, audience: $('broadcast-audience').value, user_ids: [...selected] });
        preview(job); await loadJobs();
      });
    });
    $('broadcast-consent').addEventListener('change', () => { $('broadcast-start').disabled = busy || !$('broadcast-consent').checked; });
    $('broadcast-start').addEventListener('click', async () => {
      if (busy || !draft || !$('broadcast-consent').checked) return;
      busy = true; $('broadcast-start').disabled = true;
      try { await api(`/broadcasts/${draft.id}/start`, 'POST', { confirm_consent: true, expected_count: draft.total }); $('broadcast-preview').close(); toast('Рассылка поставлена в очередь. Результаты появятся в истории.'); await loadJobs(); }
      catch (e) { $('broadcast-launch-result').textContent = e.message; }
      finally { busy = false; $('broadcast-start').disabled = !$('broadcast-consent').checked; }
    });
    $('broadcast-test-form').addEventListener('submit', (e) => { e.preventDefault(); if (!draft) return; request($('broadcast-test'), async () => { $('broadcast-test-result').textContent = 'Отправляем тест…'; try { await api(`/broadcasts/${draft.id}/test`, 'POST', { user_id: Number($('broadcast-test-user').value) }); $('broadcast-test-result').textContent = 'Тест отправлен. Проверьте личные сообщения VK.'; } catch (error) { $('broadcast-test-result').textContent = error.message; } }); });
    async function loadLog() {
      const j = await api(`/broadcasts/${logId}?page=${logPage}`);
      $('broadcast-log-body').innerHTML = `<h3>${esc(j.title)}</h3><p>${badge(j.status)} · ${j.total} получателей</p><div class="table-wrap"><table><thead><tr><th>Получатель</th><th>Результат</th><th>Подробности</th></tr></thead><tbody>${j.recipients.map((r) => `<tr><td><a href="https://vk.ru/id${r.user_id}" target="_blank" rel="noopener noreferrer">${r.user_id} ↗</a></td><td>${badge(r.status)}</td><td class="crm-log-error">${esc(r.error || '—')}</td></tr>`).join('')}</tbody></table></div><p class="hint">«Доставлено VK» означает, что API принял сообщение, а не что клиент его прочитал.</p>`;
      $('broadcast-log-page').textContent = `${logPage} / ${Math.max(1, Math.ceil(j.total / 50))}`;
      $('broadcast-log-prev').disabled = logPage <= 1; $('broadcast-log-next').disabled = logPage * 50 >= j.total;
      if (!$('broadcast-log').open) $('broadcast-log').showModal();
    }
    $('broadcast-jobs').addEventListener('click', (e) => {
      const b = e.target.closest('[data-job]'); if (!b) return;
      request(b, async () => {
        const { job: id, action } = b.dataset;
        if (action === 'log') { logId = id; logPage = 1; await loadLog(); return; }
        if (action === 'preview') { preview(await api(`/broadcasts/${id}`)); return; }
        if (action === 'cancel' && !confirm('Отменить оставшиеся отправки? Уже отправленные сообщения удалить нельзя. Текущая отправка может успеть завершиться.')) return;
        if (action === 'resume' && !confirm('Продолжить отправку оставшимся получателям? Если VK ограничил отправку, сначала устраните причину.')) return;
        await api(`/broadcasts/${id}/${action}`, 'POST'); toast(action === 'pause' ? 'Пауза. Уже начатая отправка может завершиться.' : 'Статус обновлён.'); await loadJobs();
      });
    });
    $('broadcast-log-prev').addEventListener('click', () => { logPage--; request(null, loadLog); });
    $('broadcast-log-next').addEventListener('click', () => { logPage++; request(null, loadLog); });
    $('broadcast-refresh').addEventListener('click', (e) => request(e.currentTarget, loadJobs));
    setInterval(() => { if (!document.hidden && !$('broadcast-preview').open && jobs.some((j) => ['queued', 'running'].includes(j.status))) request(null, loadJobs); }, 5000);
    request(null, loadJobs);
  }
})();
