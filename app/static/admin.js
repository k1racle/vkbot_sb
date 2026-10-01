/* Shared, dependency-free admin UI. */
window.Admin = {
  moscowDate(value) {
    if (!value) return '—';
    const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value.replace(' ', 'T') + 'Z';
    return new Date(normalized).toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'}) + ' МСК';
  },
  journey(items) {
    if (!items?.length) return '';
    const labels = {input: 'Ответ / событие', entered: 'Вход в блок', waiting: 'Ожидание', completed: 'Сценарий завершён', handoff: 'Передано менеджеру', error: 'Ошибка выполнения'};
    const types = {start: 'Начало', message: 'Сообщение', question: 'Вопрос', contact: 'Контакты', condition: 'Условие', phone_condition: 'Телефон указан?', end: 'Завершение', operator: 'Менеджер', promo: 'Промокод', wait: 'Ожидание', wait_reply: 'Ожидание ответа', set_variable: 'Запись ответа', variable_condition: 'Проверка ответа', random: 'Случайный ответ', schedule: 'Расписание', tag: 'Метка', tag_condition: 'Проверка метки', call_subflow: 'Вызов подцепочки', subflow: 'Подцепочка', return: 'Возврат'};
    return `<details class="journey"><summary>Прохождение сценария · ${Admin.escape(items[0].scenario || 'Сценарий')} · версия ${Admin.escape(items[0].version || '—')}</summary><ol>${items.map(s => `<li><small class="muted">${Admin.escape(Admin.moscowDate(s.date))}</small><div><strong>${Admin.escape(labels[s.phase] || s.phase)}</strong>${s.node ? ' · ' + Admin.escape(s.node === 'Блок' ? types[s.type] || s.node : s.node) : ''}</div>${s.detail ? `<p>${Admin.escape(s.detail)}</p>` : ''}</li>`).join('')}</ol></details>`;
  },
  get projectPrefix() {
    return document.body.dataset.projectPrefix || "";
  },
  get projectId() {
    return document.body.dataset.projectId || "";
  },
  storageKey(key) {
    return `vk-admin:project:${encodeURIComponent(Admin.projectId || "global")}:${key}`;
  },
  escape(value) {
    return String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  },
  icon(name) {
    if (!/^[a-z][a-z0-9-]*$/.test(name)) return "";
    return `<svg class="icon" aria-hidden="true" focusable="false"><use href="/static/icons.svg?v=3#${name}"></use></svg>`;
  },
  async api(path, method = "GET", body) {
    const headers = {
      "X-CSRF-Token": document.querySelector("meta[name=csrf-token]")?.content || "",
    };
    if (body !== undefined && !(body instanceof FormData))
      headers["Content-Type"] = "application/json";
    const response = await fetch(Admin.projectPrefix + "/admin/api" + path, {
      method,
      headers,
      body:
        body === undefined
          ? undefined
          : body instanceof FormData
            ? body
            : JSON.stringify(body),
    });
    let result;
    try {
      result = await response.json();
    } catch {
      throw new Error("Не удалось получить ответ сервера. Попробуйте ещё раз.");
    }
    if (!response.ok) {
      const detail = result.detail;
      throw new Error(
        Array.isArray(detail)
          ? detail
              .map((e) =>
                typeof e === "string" ? e : `${e.loc?.join(" / ")}: ${e.msg}`,
              )
              .join("\n")
          : detail || "Ошибка сервера",
      );
    }
    return result;
  },
  toast(message) {
    const target = document.getElementById("toast");
    target.textContent = message;
    target.hidden = false;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => (target.hidden = true), 5000);
  },
};
const query = new URLSearchParams(location.search);
const projectSelect = document.getElementById("project-select");
if (projectSelect) {
  const current = projectSelect.value;
  projectSelect.addEventListener("change", () => {
    const destination = projectSelect.value;
    // Keep the current name if an unsaved-changes prompt cancels navigation.
    projectSelect.value = current;
    if (destination !== current) location.assign(destination);
  });
}
function revealProjectSettings() {
  const id = location.hash.slice(1) || (query.has("project_id") ? `project-${query.get("project_id")}` : "");
  const card = document.getElementById(id);
  if (!card?.classList.contains("project-card")) return;
  const details = card.querySelector(".project-settings");
  if (details) details.open = true;
  card.scrollIntoView({ block: "start" });
}
revealProjectSettings();
window.addEventListener("hashchange", revealProjectSettings);
document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.hidden = false;
  button.addEventListener("click", async () => {
    const input = document.getElementById(button.dataset.copyTarget);
    if (!input) return;
    try {
      await navigator.clipboard.writeText(input.value);
      Admin.toast("Адрес Callback API скопирован.");
    } catch {
      input.focus();
      input.select();
      Admin.toast("Адрес выделен. Скопируйте его вручную.");
    }
  });
});
const notices = {
  saved: "Настройки сохранены.",
  project_created: "Проект создан на паузе. Настройте подключение перед включением.",
  project_saved: "Настройки проекта сохранены.",
  project_checked: "Подключение проверено: токен соответствует сообществу.",
  campaign_saved: "Кампания сохранена.",
  campaign_deleted: "Кампания перемещена в корзину. Ожидающие подарки отменены; историю и файлы сохранили.",
  campaign_toggled: "Статус кампании изменён.",
  test_sent: "Тестовое сообщение отправлено в VK.",
};
const failures = {
  chat_url: "Укажите полную HTTPS-ссылку на чат (до 500 символов, без пробелов, логина и пароля) или оставьте поле пустым. Настройки не сохранены.",
  operators: "Укажите до 50 числовых ID менеджеров через запятую или с новой строки. Настройки не сохранены.",
  no_user: "Укажите числовой ID тестового пользователя.",
  no_campaign: "Сначала сохраните и выберите кампанию.",
  vk: "VK не принял сообщение. Проверьте токен и разрешение получателя на сообщения сообщества.",
  duplicate: "Для этого поста уже есть кампания. Выберите её в списке.",
  duplicate_general: "Общая кампания уже есть. Выберите её в списке: создавать вторую не нужно.",
  post_id: "Укажите положительный числовой ID поста или оставьте поле пустым для всех публикаций.",
  size: "Размер файла должен быть не больше 50 МБ.",
  type: "Этот тип файла не поддерживается.",
  empty: "Выберите непустой файл.",
  invitation: "Добавьте от 1 до 10 приглашений, до 2000 символов каждое. В каждом тексте нужна переменная {chat_url} для ссылки на чат.",
  promo: "Для режима с подарком заполните промокод, ссылку на магазин и текст сообщения. Для обычного диалога выберите «Пригласить в чат — без подарка».",
};
const notice = document.getElementById("page-notice");
for (const [key, value] of query) {
  if (notices[key]) {
    notice.textContent = notices[key];
    notice.hidden = false;
  } else if (key.endsWith("_error")) {
    notice.textContent =
      failures[value] || "Не удалось сохранить. Проверьте поля и повторите.";
    notice.classList.add("error");
    notice.hidden = false;
  }
}
document.querySelectorAll("form[data-confirm]").forEach((form) =>
  form.addEventListener("submit", (e) => {
    if (!confirm(form.dataset.confirm)) e.preventDefault();
  }),
);
let formDirty = false;
const dirtyForms = new Set();
document.querySelectorAll("form[data-unsaved]").forEach((form) => {
  form.addEventListener("input", () => dirtyForms.add(form));
  form.addEventListener("submit", (event) => {
    if (!event.defaultPrevented) {
      dirtyForms.delete(form);
      formDirty = false;
    }
  });
});
window.addEventListener("beforeunload", (e) => {
  if (formDirty || dirtyForms.size) {
    e.preventDefault();
    e.returnValue = "";
  }
});
const statusNames = {
  sent: "Отправлено",
  failed: "Ошибка",
  received: "Получено",
  too_short: "Короткий комментарий",
  stop_word: "Стоп-слово",
  plus_word_missing: "Нет плюс-слова",
  already_sent: "Уже выдан",
  not_member: "Нет подписки",
  campaign_disabled: "Кампания выключена",
  test_filtered: "Тестовый фильтр",
  post_filtered: "Другой пост",
  no_campaign: "Нет кампании",
  waiting_chat: "Ждём получения в чате",
  waiting_subscription: "Ждём подписку",
  waiting_permission: "Нужно разрешение на сообщения",
  invite_duplicate: "Приглашение уже создано",
  invite_unsupported: "Нельзя пригласить под видео",
  video_invited_dm: "Приглашение отправлено в личку",
  video_mentioned: "Приглашение с упоминанием опубликовано под постом",
  gift_reminded_dm: "Подарок повторно отправлен в личку",
  gift_reminded_wall: "Напоминание о подарке опубликовано",
  gift_reminded_mention: "Напоминание о подарке опубликовано с упоминанием",
  gift_reminder_unavailable: "Напоминание недоступно",
  chat_mentioned: "Приглашение с упоминанием опубликовано под постом",
  mention_sending: "Публикация приглашения с упоминанием",
  video_waiting_chat: "Видео: нужен переход в чат",
  gift_unavailable: "Акция недоступна",
  gift_failed: "Ошибка выдачи в чате",
  gift_cancelled: "Подарок отменён: кампания без выдачи",
  chat_inviting: "Отправка приглашения в диалог",
  chat_invited: "Приглашение в диалог опубликовано",
  chat_invited_dm: "Приглашение в диалог отправлено в личку",
  chat_invite_duplicate: "Уже приглашён в этой кампании",
  chat_invite_unavailable: "Нет доступа для приглашения",
};
document.querySelectorAll("[data-status]").forEach((el) => {
  const original = el.textContent;
  el.textContent = original.replace(
    el.dataset.status,
    statusNames[el.dataset.status] || el.dataset.status,
  );
  if (el.dataset.status === "sent") el.classList.add("live");
  if (["failed", "gift_failed", "invite_unsupported", "chat_invite_unavailable"].includes(el.dataset.status)) el.classList.add("warning");
});
// Each invitation is a separate ordinary form field; the server also validates
// the count/length/placeholder. Keep unsaved changes when switching delivery mode.
const deliveryMode = document.getElementById("delivery-mode");
if (deliveryMode) {
  const section = document.getElementById("invitation-settings");
  const variants = document.getElementById("invitation-variants");
  const add = document.getElementById("add-invitation");
  const defaults = JSON.parse(document.getElementById("invitation-defaults").textContent);
  const template = variants.firstElementChild.cloneNode(true);
  const drafts = {};
  let variantMode = deliveryMode.value === "chat_only" ? "chat" : "gift";
  const refresh = () => {
    const chatOnly = deliveryMode.value === "chat_only";
    const nextMode = chatOnly ? "chat" : "gift";
    if (nextMode !== variantMode) {
      // Keep unsaved invitation texts for both modes during this edit.
      drafts[variantMode] = [...variants.querySelectorAll("textarea")].map(area => area.value);
      variants.replaceChildren(...(drafts[nextMode] || defaults[nextMode]).map(value => {
        const card = template.cloneNode(true);
        card.querySelector("textarea").value = value;
        return card;
      }));
      variantMode = nextMode;
    }
    const cards = [...variants.children];
    const enabled = deliveryMode.value !== "direct";
    section.hidden = !enabled;
    document.querySelectorAll("[data-gift-invitation]").forEach(el => el.hidden = chatOnly);
    document.querySelectorAll("[data-chat-invitation]").forEach(el => el.hidden = !chatOnly);
    const promo = document.getElementById("campaign-promo-fields");
    promo.hidden = promo.disabled = chatOnly;
    document.getElementById("campaign-repeat-label").textContent = chatOnly
      ? "Одно приглашение клиенту в этой кампании" : "Один промокод клиенту в этой кампании";
    document.getElementById("campaign-membership-hint").textContent = chatOnly
      ? "Подписка не обязательна. При необходимости проверьте её в сценарии."
      : "Подписка на сообщество проверяется перед выдачей подарка.";
    cards.forEach((card, i) => {
      card.querySelector("[data-variant-title]").textContent = `Вариант ${i + 1}`;
      card.querySelector("[data-variant-label]").textContent = `Текст приглашения ${i + 1}`;
      card.querySelector("[data-remove-invitation]").disabled = cards.length <= 1;
      const area = card.querySelector("textarea");
      area.required = enabled;
      area.setCustomValidity(enabled && !area.value.includes("{chat_url}")
        ? "Добавьте {chat_url}, чтобы человек мог перейти в чат." : "");
    });
    add.disabled = cards.length >= 10;
  };
  deliveryMode.addEventListener("change", refresh);
  variants.addEventListener("input", refresh);
  variants.addEventListener("click", (e) => {
    const button = e.target.closest("[data-remove-invitation]");
    if (!button || variants.children.length <= 1) return;
    button.closest(".invitation-variant").remove();
    formDirty = true;
    refresh();
  });
  add.addEventListener("click", () => {
    if (variants.children.length >= 10) return;
    const card = variants.firstElementChild.cloneNode(true);
    card.querySelector("textarea").value = "Спасибо за комментарий! 🎁 Ваш подарок здесь: {chat_url}\nНажмите «Начать» или напишите «Подарок».";
    variants.append(card);
    formDirty = true;
    refresh();
    card.querySelector("textarea").focus();
  });
  refresh();
}
async function loadClients() {
  const list = document.getElementById("clients-list");
  if (!list) return;
  try {
    const clients = await Admin.api("/conversations");
    const e = Admin.escape;
    list.innerHTML = clients.length
      ? clients
          .map(
            (c) =>
              `<article class="panel"><div class="panel-heading"><div><h3>${e(c.name)}</h3><a class="small" href="https://vk.com/id${c.user_id}" target="_blank" rel="noopener">id${c.user_id} ${Admin.icon("external-link")}</a></div><span class="badge ${c.handoff ? "warning" : "live"}">${c.handoff ? (c.assigned_operator_id ? "В работе у менеджера" : "Ждёт менеджера") : "Бот"}</span></div>${c.assigned_operator_id ? `<p>Ответственный: <a href="https://vk.com/id${c.assigned_operator_id}" target="_blank" rel="noopener">id${c.assigned_operator_id} ${Admin.icon("external-link")}</a><span class="hint"> · с ${e(c.assigned_at)} UTC</span></p>` : ""}${c.handoff ? `<button class="btn secondary" data-resume="${c.user_id}">Вернуть к боту</button>` : ""}<details><summary>Ответы клиента</summary>${Object.entries(
                c.variables,
              )
                .map(([k, v]) => `<p><strong>${e(k)}:</strong> ${e(v)}</p>`)
                .join(
                  "",
                )}</details><details open><summary>Сообщения и обращения</summary>${c.events.map((m) => `<div class="event-message">${e(m.text) || "[без текста]"}<div class="hint">${e(Admin.moscowDate(m.date))} · ${m.kind === "wait" ? "Таймер сценария" : m.kind === "operator_reply" ? "Ответ менеджера" : m.kind === "operator_claim" ? "Взять в работу" : m.kind === "gift_join" ? "Выдача после подписки" : m.kind === "gift" ? "Получение подарка" : "Входящее"} · ${m.status === "failed" ? "Ошибка" : m.status === "retry" ? "Повторная попытка" : m.status === "cancelled" ? "Отменено" : m.status === "waiting_permission" ? "Ждём разрешение" : "Обработано"}</div>${Admin.journey(m.journey)}${m.error ? `<div class="notice error">${e(m.error)}</div>` : ""}</div>`).join("")}</details></article>`,
          )
          .join("")
      : `<div class="panel empty"><h2>Диалоги ещё не начались</h2><p>Опубликуйте сценарий и напишите сообществу в VK. Здесь появятся клиенты и их обращения.</p><a class="btn secondary" href="${e(Admin.projectPrefix)}/admin?section=scenarios">Открыть сценарии</a></div>`;
    list.querySelectorAll(':scope > article').forEach((card, index) => {
      if (clients[index].tags?.length) {
        const tags = document.createElement('p');
        tags.innerHTML = clients[index].tags.map(t => `<span class="badge">${e(t)}</span>`).join(' ');
        card.insertBefore(tags, card.querySelector('details'));
      }
      const waiting = clients[index].wait;
      if (!waiting) return;
      const box = document.createElement('div');
      box.className = 'notice';
      const labels = { pending: 'Ожидание до', done: 'Ожидание завершено', cancelled: 'Ожидание отменено', failed: 'Ошибка ожидания' };
      if (waiting.contact_reminder) labels.pending = 'Напоминание о контакте';
      box.innerHTML = `${Admin.icon('clock')} <strong>${e(labels[waiting.status] || waiting.status)}</strong>${waiting.status === 'pending' ? ` ${e(new Date(waiting.due_at).toLocaleString('ru-RU'))} (время вашего устройства)` : ''}${waiting.error ? `<div class="hint">${e(waiting.error)}</div>` : ''}`;
      if (waiting.status === 'pending') {
        const cancel = document.createElement('button');
        cancel.className = 'btn secondary small';
        cancel.textContent = waiting.contact_reminder ? 'Отменить напоминание' : 'Отменить ожидание';
        cancel.onclick = async () => {
          if (!confirm(waiting.contact_reminder ? 'Отменить напоминание? Бот продолжит принимать контакт без повторных напоминаний.' : 'Отменить отложенное продолжение? Клиент сможет начать диалог заново через «Меню».')) return;
          cancel.disabled = true;
          try {
            await Admin.api(`/conversations/${clients[index].user_id}/cancel-wait`, 'POST');
            await loadClients();
          } catch (error) { Admin.toast(error.message); cancel.disabled = false; }
        };
        box.append(document.createElement('br'), cancel);
      }
      card.insertBefore(box, card.querySelector('details'));
    });
    list.querySelectorAll("[data-resume]").forEach(
      (b) =>
        (b.onclick = async () => {
          try {
            b.disabled = true;
            await Admin.api(
              `/conversations/${b.dataset.resume}/resume`,
              "POST",
            );
            await loadClients();
            Admin.toast(
              "Бот продолжит диалог со следующего сообщения клиента.",
            );
          } catch (error) {
            Admin.toast(error.message);
            b.disabled = false;
          }
        }),
    );
  } catch (error) {
    list.textContent = error.message;
  }
}
if (document.getElementById("clients-list")) {
  loadClients();
  document.getElementById("refresh-clients").onclick = loadClients;
}

const diagnosticButton = document.getElementById('check-vk');
if (diagnosticButton) diagnosticButton.onclick = async () => {
  const output = document.getElementById('vk-diagnostics');
  diagnosticButton.disabled = true;
  output.textContent = 'Проверяем подключение к VK…';
  try {
    const report = await Admin.api('/vk-diagnostics', 'POST');
    const e = Admin.escape, labels = {ok: '✓', warning: 'Проверить', error: 'Ошибка'};
    output.innerHTML = `<p class="hint">Проверено: ${e(Admin.moscowDate(report.checked_at))}</p><p><strong>Последнее событие от VK:</strong> ${report.last_callback ? `${e(Admin.moscowDate(report.last_callback.date))} · ${e(report.last_callback.type)}<br>${e(report.last_callback.reason)}` : 'Пока не зарегистрировано. После обновления оставьте новый комментарий или напишите сообществу.'}</p><div class="table-wrap"><table><thead><tr><th>Проверка</th><th>Результат</th><th>Подробности</th></tr></thead><tbody>${report.checks.map(c => `<tr><td>${e(c.name)}</td><td>${e(labels[c.status])}</td><td>${e(c.detail)}</td></tr>`).join('')}</tbody></table></div><p class="hint">Проверка прав не публикует пробный ответ. Для окончательной проверки оставьте новый тестовый комментарий.</p>`;
  } catch (error) {
    output.textContent = error.message;
  } finally {
    diagnosticButton.disabled = false;
  }
};
