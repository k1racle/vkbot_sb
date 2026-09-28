(() => {
  const container = document.getElementById('trash-items');
  if (!container) return;
  const names = { campaign: 'Кампания', scenario: 'Сценарий', broadcast: 'Рассылка' };
  const esc = Admin.escape;
  async function load() {
    const data = await Admin.api('/trash');
    container.innerHTML = data.items.map(item => `<div class="panel-heading spaced"><div><strong>${esc(item.title || 'Без названия')}</strong><p class="hint">${esc(names[item.kind])} · ID ${esc(String(item.id))}</p></div><button class="btn secondary small" data-kind="${esc(item.kind)}" data-id="${esc(String(item.id))}">${Admin.icon("rotate-ccw")} Восстановить</button></div>`).join('') || '<p class="muted">Корзина пуста.</p>';
  }
  container.addEventListener('click', async event => {
    const button = event.target.closest('[data-kind]');
    if (!button || button.disabled || !confirm('Восстановить объект без включения и отправки сообщений?')) return;
    button.disabled = true;
    try {
      await Admin.api(`/trash/${button.dataset.kind}/${encodeURIComponent(button.dataset.id)}/restore`, 'POST');
      await load();
      Admin.toast('Восстановлено. Откройте соответствующий раздел проекта.');
    } catch (error) { Admin.toast(error.message); button.disabled = false; }
  });
  load().catch(error => { container.textContent = error.message; });
})();
