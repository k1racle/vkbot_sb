/* Shared, dependency-free admin UI. */
window.Admin = {
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
  async api(path, method = "GET", body) {
    const headers = {
      "X-CSRF-Token": document.querySelector("meta[name=csrf-token]").content,
    };
    if (body !== undefined && !(body instanceof FormData))
      headers["Content-Type"] = "application/json";
    const response = await fetch("/admin/api" + path, {
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
const notices = {
  saved: "Настройки сохранены.",
  campaign_saved: "Кампания сохранена.",
  campaign_deleted: "Кампания удалена.",
  campaign_toggled: "Статус кампании изменён.",
  test_sent: "Тестовое сообщение отправлено в VK.",
};
const failures = {
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
document.querySelectorAll("form[data-unsaved]").forEach((form) => {
  form.addEventListener("input", () => (formDirty = true));
  form.addEventListener("submit", () => (formDirty = false));
});
window.addEventListener("beforeunload", (e) => {
  if (formDirty) {
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
  already_sent: "Уже выдан",
  not_member: "Нет подписки",
  campaign_disabled: "Кампания выключена",
  test_filtered: "Тестовый фильтр",
  post_filtered: "Другой пост",
  no_campaign: "Нет кампании",
};
document.querySelectorAll("[data-status]").forEach((el) => {
  const original = el.textContent;
  el.textContent = original.replace(
    el.dataset.status,
    statusNames[el.dataset.status] || el.dataset.status,
  );
  if (el.dataset.status === "sent") el.classList.add("live");
  if (el.dataset.status === "failed") el.classList.add("warning");
});
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
              `<article class="panel"><div class="panel-heading"><div><h3>${e(c.name)}</h3><a class="small" href="https://vk.com/id${c.user_id}" target="_blank" rel="noopener">id${c.user_id} ↗</a></div><span class="badge ${c.handoff ? "warning" : "live"}">${c.handoff ? (c.assigned_operator_id ? "В работе у менеджера" : "Ждёт менеджера") : "Бот"}</span></div>${c.assigned_operator_id ? `<p>Ответственный: <a href="https://vk.com/id${c.assigned_operator_id}" target="_blank" rel="noopener">id${c.assigned_operator_id} ↗</a><span class="hint"> · с ${e(c.assigned_at)} UTC</span></p>` : ""}${c.handoff ? `<button class="btn secondary" data-resume="${c.user_id}">Вернуть к боту</button>` : ""}<details><summary>Ответы клиента</summary>${Object.entries(
                c.variables,
              )
                .map(([k, v]) => `<p><strong>${e(k)}:</strong> ${e(v)}</p>`)
                .join(
                  "",
                )}</details><details open><summary>Сообщения и обращения</summary>${c.events.map((m) => `<div class="event-message">${e(m.text) || "[без текста]"}<div class="hint">${e(m.date)} · ${m.kind === "operator_reply" ? "Ответ менеджера" : m.kind === "operator_claim" ? "Взять в работу" : "Входящее"} · ${m.status === "failed" ? "Ошибка" : "Обработано"}</div>${m.error ? `<div class="notice error">${e(m.error)}</div>` : ""}</div>`).join("")}</details></article>`,
          )
          .join("")
      : '<div class="panel empty"><h2>Диалоги ещё не начались</h2><p>Опубликуйте сценарий и напишите сообществу в VK. Здесь появятся клиенты и их обращения.</p><a class="btn secondary" href="/admin?section=scenarios">Открыть сценарии</a></div>';
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
