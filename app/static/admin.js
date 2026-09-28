/* Shared, dependency-free admin UI. */
window.Admin = {
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
    return `<svg class="icon" aria-hidden="true" focusable="false"><use href="/static/icons.svg?v=1#${name}"></use></svg>`;
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
  video_waiting_chat: "Видео: нужен переход в чат",
  gift_unavailable: "Акция недоступна",
  gift_failed: "Ошибка выдачи в чате",
};
document.querySelectorAll("[data-status]").forEach((el) => {
  const original = el.textContent;
  el.textContent = original.replace(
    el.dataset.status,
    statusNames[el.dataset.status] || el.dataset.status,
  );
  if (el.dataset.status === "sent") el.classList.add("live");
  if (["failed", "gift_failed", "invite_unsupported"].includes(el.dataset.status)) el.classList.add("warning");
});
// Each invitation is a separate ordinary form field; the server also validates
// the count/length/placeholder. Keep unsaved changes when switching delivery mode.
const deliveryMode = document.getElementById("delivery-mode");
if (deliveryMode) {
  const section = document.getElementById("invitation-settings");
  const variants = document.getElementById("invitation-variants");
  const add = document.getElementById("add-invitation");
  const refresh = () => {
    const cards = [...variants.children];
    const enabled = deliveryMode.value === "chat_invite";
    section.hidden = !enabled;
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
                )}</details><details open><summary>Сообщения и обращения</summary>${c.events.map((m) => `<div class="event-message">${e(m.text) || "[без текста]"}<div class="hint">${e(m.date)} · ${m.kind === "operator_reply" ? "Ответ менеджера" : m.kind === "operator_claim" ? "Взять в работу" : m.kind === "gift_join" ? "Выдача после подписки" : m.kind === "gift" ? "Получение подарка" : "Входящее"} · ${m.status === "failed" ? "Ошибка" : m.status === "waiting_permission" ? "Ждём разрешение" : "Обработано"}</div>${m.error ? `<div class="notice error">${e(m.error)}</div>` : ""}</div>`).join("")}</details></article>`,
          )
          .join("")
      : `<div class="panel empty"><h2>Диалоги ещё не начались</h2><p>Опубликуйте сценарий и напишите сообществу в VK. Здесь появятся клиенты и их обращения.</p><a class="btn secondary" href="${e(Admin.projectPrefix)}/admin?section=scenarios">Открыть сценарии</a></div>`;
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
