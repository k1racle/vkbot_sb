/* Names only: no messages, callback registration or automatic project enabling. */
(() => {
  const cards = [...document.querySelectorAll('.project-card[data-project-id]')];
  async function refresh(card, automatic) {
    const button = card.querySelector('[data-refresh-name]');
    if (button.disabled) return;
    const id = card.dataset.projectId;
    const heading = card.querySelector(`#project-title-${CSS.escape(id)}`);
    const previous = heading.textContent;
    const input = card.querySelector('[name="name"]');
    const label = card.querySelector('[data-name-status]');
    button.disabled = true;
    label.textContent = 'Получаем название сообщества из VK…';
    label.classList.remove('crm-error');
    try {
      const body = new URLSearchParams({ automatic: automatic ? '1' : '0' });
      const response = await fetch(`/projects/${id}/name`, {
        method: 'POST', body,
        headers: { 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content },
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось получить название. Попробуйте позже.');
      const project = data.project;
      heading.textContent = project.name;
      const option = document.querySelector(`#project-select option[value="/p/${id}/admin"]`);
      if (option) option.textContent = project.name + (project.enabled ? '' : ' · На паузе');
      // Do not erase an edit the administrator made while VK was responding.
      if (input?.value === previous) input.value = project.name;
      label.textContent = data.updated ? 'Название обновлено в карточке и переключателе. Для проверки ключа нажмите «Проверить подключение».' : 'Название актуально или было изменено вручную. Проверка ключа — отдельной кнопкой.';
      card.dataset.autoName = '0';
    } catch (error) {
      label.textContent = error.message || 'Не удалось связаться с VK.';
      label.classList.add('crm-error');
    } finally { button.disabled = false; }
  }
  cards.forEach(card => card.querySelector('[data-refresh-name]').addEventListener('click', () => refresh(card, false)));
  // One pass per page, sequential to avoid a burst of VK requests for many groups.
  (async () => { for (const card of cards) if (card.dataset.autoName === '1') await refresh(card, true); })();
})();
