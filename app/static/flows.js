(() => {
  const $ = (id) => document.getElementById(id),
    e = Admin.escape;
  const kinds = {
    start: ["Начало", "play"],
    message: ["Сообщение", "message-square"],
    question: ["Вопрос", "circle-question-mark"],
    condition: ["Условие", "git-branch"],
    promo: ["Промокод", "tag"],
    operator: ["Менеджер", "user-round"],
    end: ["Завершение", "circle-stop"],
    contact: ["Телефон / email", "user-round"],
    phone_condition: ["Телефон указан?", "check"],
    set_variable: ["Записать переменную", "pencil"],
    variable_condition: ["Проверить ответ", "git-branch"],
    random: ["Случайный ответ", "messages-square"],
    wait: ["Ожидание", "clock"],
    wait_reply: ["Ждать ответ", "messages-square"],
    schedule: ["Расписание", "clock"],
    tag: ["Изменить метку", "tag"],
    tag_condition: ["Проверить метку", "git-branch"],
    subflow: ["Вход подцепочки", "workflow"],
    call_subflow: ["Вызвать подцепочку", "workflow"],
    return: ["Возврат", "arrow-left"],
  };
  const blockDefaults = { phone_check_mode: 'provided', reminder_enabled: false, reminder_delay_value: 3, reminder_delay_unit: 'hours', reminder_text: '', allow_later: false, later_text: '', later_reminder_enabled: false, later_reminder_delay_value: 24, later_reminder_delay_unit: 'hours', later_reminder_text: '', tag: '', tag_action: 'add', timezone: 'Europe/Moscow', weekdays: [0, 1, 2, 3, 4], time_from: '09:00', time_to: '18:00', date_from: '', date_to: '', subflow_id: '', pass_variables: [], return_variables: [] };
  const dayNames = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  let flows = [],
    campaigns = [],
    media = [],
    flow = null,
    selected = null,
    dirty = false,
    busy = false,
    pending = null,
    zoom = 0.8,
    history = [],
    previewState = {};
  const graph = () => flow.graph;
  const entryDefaults = {mode: 'default', keywords: '', match: 'contains'};
  const narrowEditor = window.matchMedia('(max-width: 760px)');
  function collapseLibrary(collapsed) {
    $('editor').classList.toggle('library-collapsed', collapsed);
    $('block-library-content').hidden = collapsed;
    $('toggle-block-library').setAttribute('aria-expanded', String(!collapsed));
    $('toggle-block-library').title = collapsed ? 'Открыть каталог блоков' : 'Свернуть каталог блоков';
    requestAnimationFrame(drawEdges);
  }
  $('toggle-block-library').onclick = () => collapseLibrary(!$('editor').classList.contains('library-collapsed'));
  narrowEditor.addEventListener('change', () => collapseLibrary(narrowEditor.matches));
  collapseLibrary(narrowEditor.matches);
  const node = () => graph().nodes.find((n) => n.id === selected);
  const snapshot = () => {
    history.push(JSON.stringify(graph()));
    if (history.length > 40) history.shift();
  };
  const get = (o, path) => path.split(".").reduce((v, k) => v?.[k], o);
  const set = (o, path, value) => {
    const bits = path.split(".");
    const last = bits.pop();
    bits.reduce((v, k) => v[k], o)[last] = value;
  };
  function markDirty() {
    dirty = true;
    $("dirty-state").textContent = "Есть изменения";
  }
  function errors(message) {
    const box = $("flow-errors");
    box.replaceChildren();
    box.hidden = !message;
    if (message)
      message.split("\n").forEach((line) => {
        const p = document.createElement("div");
        p.textContent = line;
        box.append(p);
      });
  }
  async function action(fn) {
    if (busy) return;
    busy = true;
    document
      .querySelectorAll(
        ".flow-toolbar button, .flow-toolbar select, #new-flow, #empty-create",
      )
      .forEach((b) => (b.disabled = true));
    $("editor").inert = true;
    try {
      await fn();
    } catch (err) {
      errors(err.message);
      Admin.toast("Действие не выполнено. Подробности над схемой.");
    } finally {
      busy = false;
      document
        .querySelectorAll(
          ".flow-toolbar button, .flow-toolbar select, #new-flow, #empty-create",
        )
        .forEach((b) => (b.disabled = false));
      $("editor").inert = false;
    }
  }
  function exits(n) {
    if (n.type === 'wait_reply') return [{key: 'yes', label: 'Ответил', target: n.yes}, {key: 'no', label: 'Время вышло', target: n.no}];
    if (n.type === 'call_subflow') return [{key: 'subflow_id', label: 'Подцепочка', target: n.subflow_id}, {key: 'next', label: 'После возврата', target: n.next}];
    if (["condition", "variable_condition", "schedule", "tag_condition", "phone_condition"].includes(n.type))
      return [
        { key: "yes", label: "Да", target: n.yes },
        { key: "no", label: "Нет", target: n.no },
      ];
    if (["end", "operator", "return"].includes(n.type)) return [];
    if (n.type === "message" && n.buttons.length)
      return n.buttons.map((b, i) => ({
        key: `buttons.${i}.target`,
        label: b.label,
        target: b.target,
        link: b.kind === "link",
      }));
    const output = [
      {
        key: "next",
        label: n.type === "question" ? "Другой ответ" : "Далее",
        target: n.next,
      },
    ];
    if (n.type === "question")
      output.push(
        ...n.rules.map((r, i) => ({
          key: `rules.${i}.target`,
          label: r.words || "Ветка ответа",
          target: r.target,
        })),
      );
    return output;
  }
  function canFinishExit(n, path) {
    return !['start', 'subflow'].includes(n.type) && path !== 'subflow_id';
  }
  function choose(id) {
    selected = id;
    document
      .querySelectorAll(".flow-node")
      .forEach((el) =>
        el.classList.toggle("selected", el.dataset.id === selected),
      );
    renderInspector();
    drawEdges();
  }
  function summary(n) {
    if (n.type === 'wait_reply') return `Ждать ответ: ${n.delay_value} ${waitUnits[n.delay_unit]}\nОтвет → {${n.variable}}`;
    if (n.type === 'schedule') return `${n.weekdays.map(d => dayNames[d]).join(', ')} · ${n.time_from}–${n.time_to}\n${n.timezone}`;
    if (n.type === 'tag') return `${n.tag_action === 'add' ? 'Добавить' : 'Снять'} метку: ${n.tag || 'укажите справа'}`;
    if (n.type === 'tag_condition') return `Есть метка «${n.tag || 'укажите справа'}»?`;
    if (n.type === 'subflow') return 'Повторно используемая цепочка. Вход — через блок вызова.';
    if (n.type === 'call_subflow') return graph().nodes.find(x => x.id === n.subflow_id)?.title || 'Выберите подцепочку справа';
    if (n.type === 'return') return 'Вернуться туда, откуда вызвана подцепочка';
    if (n.type === 'wait') return `Пауза: ${n.delay_value} ${waitUnits[n.delay_unit]}\nЗатем — следующий блок`;
    if (n.type === 'contact') return `${n.contact_type === 'email' ? 'Email' : 'Телефон'} → {${n.variable}}\n${n.text}${n.reminder_enabled ? `\nНапомнить: ${n.reminder_delay_value} ${waitUnits[n.reminder_delay_unit]}` : ''}${n.allow_later ? '\nКнопка «Позже»' : ''}`;
    if (n.type === 'phone_condition') return n.phone_check_mode === 'any' ? 'Есть сохранённый телефон, в том числе из VK?' : 'Клиент уже оставил телефон боту?';
    if (n.type === 'set_variable') return `{${n.variable}} = ${n.value || '(пусто)'}`;
    if (n.type === 'variable_condition') return `Проверка {${n.variable}}: ${comparisonNames[n.comparison]} ${['empty', 'not_empty'].includes(n.comparison) ? '' : n.value}`;
    if (n.type === 'random') return `${n.variants.length} вариантов · один ответ за шаг\n${n.variants[0] || 'Добавьте тексты справа'}`;
    if (n.type === 'condition') return n.condition === 'member' ? 'Подписан на сообщество?' : n.condition === 'promo_sent' ? 'Уже получал промокод?' : `Содержит: ${n.words || 'укажите слова'}`;
    if (n.type === 'start') return graph().entry?.mode === 'keywords'
      ? `Запуск по фразам: ${graph().entry.keywords || 'укажите справа'}`
      : 'По умолчанию: первое сообщение или команда «меню»';
    if (n.type === 'promo') return campaigns.find(c => c.id === n.campaign_id)?.title || 'Выберите кампанию';
    return n.text || 'Нажмите, чтобы настроить';
  }
  const comparisonNames = { equals: 'равно', not_equals: 'не равно', contains: 'содержит', empty: 'не заполнено', not_empty: 'заполнено', gt: 'больше', gte: 'больше или равно', lt: 'меньше', lte: 'меньше или равно' };
  const waitUnits = { seconds: 'сек.', minutes: 'мин.', hours: 'ч.', days: 'дн.' };
  function renderNodes() {
    if (!flow) return;
    const width = Math.max(1800, ...graph().nodes.map((n) => n.x + 400)),
      height = Math.max(950, ...graph().nodes.map((n) => n.y + 500));
    $("canvas-world").style.width = width + "px";
    $("canvas-world").style.height = height + "px";
    $("flow-nodes").innerHTML = graph()
      .nodes.map(
        (n) =>
          `<article class="flow-node ${n.id === selected ? "selected" : ""}" data-id="${e(n.id)}" data-kind="${n.type}" style="left:${n.x}px;top:${n.y}px" tabindex="0" aria-label="${e(n.title)}"><button class="node-port input" aria-label="Соединить с ${e(n.title)}" data-input="${e(n.id)}"></button><div class="node-heading"><span class="node-symbol">${Admin.icon(kinds[n.type][1])}</span><strong>${e(n.title)}</strong><small>${kinds[n.type][0]}</small></div><div class="node-content">${e(summary(n))}${n.media_id ? `\n${Admin.icon("paperclip")} Прикреплён файл` : ""}</div>${exits(
            n,
          )
            .map(
              (o) =>
                `<div class="node-output"><span>${o.link ? Admin.icon("external-link") + " " : ""}${e(o.label)}${!o.link && !o.target && canFinishExit(n, o.key) ? ' · конец' : ''}</span>${!o.link ? `<button class="node-port ${pending?.id === n.id && pending?.key === o.key ? "pending" : ""}" data-output="${e(o.key)}" aria-label="Переход ${e(o.label)}"></button>` : ""}</div>`,
            )
            .join("")}</article>`,
      )
      .join("");
    document.querySelectorAll(".flow-node").forEach((el) => {
      el.addEventListener("click", (ev) => {
        if (!ev.target.closest(".node-port")) choose(el.dataset.id);
      });
      el.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") choose(el.dataset.id);
      });
      el.querySelector("[data-input]").onclick = (ev) => {
        ev.stopPropagation();
        if (pending) {
          snapshot();
          set(
            graph().nodes.find((n) => n.id === pending.id),
            pending.key,
            el.dataset.id,
          );
          pending = null;
          markDirty();
          renderNodes();
          renderInspector();
          $("canvas-hint").textContent =
            "Связь добавлена. Её можно изменить в настройках блока.";
        }
      };
      el.querySelectorAll("[data-output]").forEach(
        (p) =>
          (p.onclick = (ev) => {
            ev.stopPropagation();
            pending = { id: el.dataset.id, key: p.dataset.output };
            $("canvas-hint").textContent =
              "Теперь нажмите на круг слева у следующего блока. Escape — отменить.";
            renderNodes();
          }),
      );
      const handle = el.querySelector(".node-heading");
      handle.onpointerdown = (ev) => {
        if (ev.button !== 0) return;
        choose(el.dataset.id);
        snapshot();
        const n = node(),
          sx = ev.clientX,
          sy = ev.clientY,
          ox = n.x,
          oy = n.y;
        handle.setPointerCapture(ev.pointerId);
        handle.onpointermove = (move) => {
          n.x = Math.max(0, Math.min(9500, ox + (move.clientX - sx) / zoom));
          n.y = Math.max(0, Math.min(9500, oy + (move.clientY - sy) / zoom));
          el.style.left = n.x + "px";
          el.style.top = n.y + "px";
          markDirty();
          drawEdges();
        };
        handle.onpointerup = () => {
          handle.onpointermove = null;
          handle.onpointerup = null;
          renderNodes();
        };
        handle.onpointercancel = () => {
          handle.onpointermove = null;
          renderNodes();
        };
      };
    });
    requestAnimationFrame(drawEdges);
  }
  function drawEdges() {
    if (!flow) return;
    const lines = [];
    for (const n of graph().nodes) {
      const el = document.querySelector(
        `.flow-node[data-id="${CSS.escape(n.id)}"]`,
      );
      if (!el) continue;
      for (const o of exits(n)) {
        if (!o.target || o.link) continue;
        const target = graph().nodes.find((x) => x.id === o.target);
        const port = el.querySelector(`[data-output="${CSS.escape(o.key)}"]`);
        if (!target || !port) continue;
        const x = n.x + 255,
          y =
            n.y +
            port.parentElement.offsetTop +
            port.parentElement.offsetHeight / 2,
          tx = target.x,
          ty = target.y + 26,
          bend = Math.max(60, Math.abs(tx - x) * 0.45);
        lines.push(
          `<path class="edge ${selected === n.id ? "active" : ""}" d="M${x},${y} C${x + bend},${y} ${tx - bend},${ty} ${tx},${ty}"/>`,
        );
      }
    }
    $("edge-lines").innerHTML = lines.join("");
  }
  function targetSelect(path, label, value) {
    const emptyLabel = canFinishExit(node(), path) ? 'Закончить без сообщения' : 'Выберите блок…';
    return `<label>${e(label)}<select data-field="${path}"><option value="">${emptyLabel}</option>${graph()
      .nodes.map(
        (n) =>
          `<option value="${e(n.id)}" ${value === n.id ? "selected" : ""}>${e(n.title)} · ${kinds[n.type][0]}</option>`,
      )
      .join("")}</select></label>`;
  }
  function campaignSelect(n) {
    return `<label>Кампания<select data-field="campaign_id"><option value="">Выберите кампанию…</option>${campaigns.map((c) => `<option value="${c.id}" ${n.campaign_id === c.id ? "selected" : ""}>${e(c.title)}${c.enabled ? "" : " (выключена)"}</option>`).join("")}</select></label><p class="hint">Используются сообщение, файл и промокод выбранной кампании.</p>`;
  }
  function contactReminderFields(n, prefix) {
    return `<label>Через сколько напомнить<input type="number" data-field="${prefix}_delay_value" min="1" max="315360000" step="1" value="${e(n[prefix + '_delay_value'])}"></label><label>Единица времени<select data-field="${prefix}_delay_unit">${Object.entries({seconds: 'Секунды', minutes: 'Минуты', hours: 'Часы', days: 'Дни'}).map(([value, label]) => `<option value="${value}" ${n[prefix + '_delay_unit'] === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label>Текст напоминания<textarea data-field="${prefix}_text" maxlength="3500" placeholder="Пусто — стандартная просьба прислать контакт">${e(n[prefix + '_text'])}</textarea></label>`;
  }
  function renderInspector() {
    if (!flow) return;
    const n = node();
    if (!n) {
      $("block-inspector").innerHTML =
        '<div class="empty"><h3>Настройки блока</h3><p>Выберите блок на схеме.</p></div>';
      return;
    }
    let html = `<span class="eyebrow">НАСТРОЙКИ БЛОКА</span><h3>${Admin.icon(kinds[n.type][1])} ${kinds[n.type][0]}</h3><label>Название блока<input data-field="title" value="${e(n.title)}" maxlength="120"></label>`;
    if (n.type === "start") {
      const entry = {...entryDefaults, ...graph().entry};
      html += `<label>Название сценария<input id="scenario-title" value="${e(flow.title)}" maxlength="120"></label><h4>Условия запуска</h4><label>Когда запускать<select data-entry="mode"><option value="default" ${entry.mode === 'default' ? 'selected' : ''}>По умолчанию</option><option value="keywords" ${entry.mode === 'keywords' ? 'selected' : ''}>По ключевым фразам</option></select></label>`;
      if (entry.mode === 'keywords') {
        html += `<label>Ключевые слова и фразы<textarea data-entry="keywords" maxlength="2000" placeholder="хочу курс&#10;записаться на курс">${e(entry.keywords)}</textarea></label><p class="hint">Каждая фраза с новой строки или через запятую. Достаточно любой одной. До 30 фраз. Пустой список не запускает сценарий.</p><label>Совпадение<select data-entry="match"><option value="contains" ${entry.match === 'contains' ? 'selected' : ''}>Фраза внутри сообщения</option><option value="exact" ${entry.match === 'exact' ? 'selected' : ''}>Сообщение целиком</option></select></label><p class="hint">Без учёта регистра, лишних пробелов и знаков препинания. «ХОЧУ КУРС!» подходит для «хочу курс», а «курс» не совпадёт с «курсы».</p><p class="notice">Ключевая фраза начинает цепочку заново и отменяет прежние ожидания клиента. Дальнейшие ответы продолжают эту цепочку. Во время общения с менеджером автоматического переключения нет.</p><p class="hint">При совпадении нескольких сценариев выбирается точное совпадение, затем самая длинная фраза. Одинаковые фразы в разных активных сценариях запрещены. Команды «Меню», «Стоп», получение подарка и вызов менеджера имеют приоритет. Кнопки текущего диалога продолжают его, а не запускают другой сценарий.</p>`;
      } else {
        html += '<p class="hint">Запускается на первое сообщение без подходящей ключевой фразы и по командам «Меню» / «Начать». В проекте один сценарий по умолчанию и несколько сценариев по фразам.</p>';
      }
      html += '<p class="hint">Условия применяются после публикации, только в этом проекте и при включённых ответах в «Общении». Предпросмотр открывает выбранную цепочку напрямую; запуск по фразе проверяйте в сообщениях сообщества.</p>';
    }
    if (['wait', 'wait_reply'].includes(n.type)) {
      html += `<label>Сколько ждать<input type="number" data-field="delay_value" min="1" max="315360000" step="1" value="${e(n.delay_value)}"></label><label>Единица времени<select data-field="delay_unit">${Object.entries({seconds: 'Секунды', minutes: 'Минуты', hours: 'Часы', days: 'Дни'}).map(([value, label]) => `<option value="${value}" ${n.delay_unit === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label><p class="hint">Например: 3 часа. От 1 секунды до 3650 дней. Таймер сохраняется при перезапуске. Сообщение добавьте отдельным следующим блоком.</p><p class="hint">${n.type === 'wait_reply' ? 'Текстовый ответ до срока выбирает ветку «Ответил». Без ответа сработает «Время вышло».' : 'Обычный ответ клиента не сокращает паузу.'} «Меню» начинает диалог заново. «Стоп», запрет сообщений и передача менеджеру отменяют ожидание.</p><p class="hint">Публикация новой версии, приостановка сценария, проекта или раздела «Общение» отменяет старые таймеры. При недоступности сервера продолжение произойдёт после его запуска, не раньше срока.</p>`;
    }
    if (n.type === 'wait_reply') html += '<p class="notice">Задаёт вопрос и ждёт текстовый ответ до срока. Успел — ветка «Ответил», не успел — «Время вышло». Вложения без текста и старые кнопки не считаются ответом. Срок не продлевается.</p>';
    if (['tag', 'tag_condition'].includes(n.type)) html += `<label>Метка клиента<input data-field="tag" value="${e(n.tag)}" maxlength="40" placeholder="интересуется доставкой"></label><p class="hint">Без учёта регистра. Метки сохраняются у клиента в этой группе даже после «Меню». Не меняют разрешения на рассылки.</p>`;
    if (n.type === 'tag') html += `<label>Действие<select data-field="tag_action"><option value="add" ${n.tag_action === 'add' ? 'selected' : ''}>Добавить метку</option><option value="remove" ${n.tag_action === 'remove' ? 'selected' : ''}>Снять метку</option></select></label>`;
    if (n.type === 'schedule') html += `<fieldset class="schedule-days"><legend>Дни недели</legend>${dayNames.map((day, index) => `<label class="check"><input type="checkbox" data-weekday="${index}" ${n.weekdays.includes(index) ? 'checked' : ''}>${day}</label>`).join('')}</fieldset><label>С начала дня, ЧЧ:ММ<input data-field="time_from" value="${e(n.time_from)}" maxlength="5" placeholder="09:00"></label><label>До, ЧЧ:ММ<input data-field="time_to" value="${e(n.time_to)}" maxlength="5" placeholder="18:00"></label><label>Часовой пояс<input data-field="timezone" value="${e(n.timezone)}" list="flow-timezones" maxlength="100"></label><datalist id="flow-timezones"><option value="Europe/Moscow"><option value="Asia/Yekaterinburg"><option value="Asia/Novosibirsk"><option value="Asia/Vladivostok"><option value="UTC"></datalist><label>С даты (необязательно)<input type="date" data-field="date_from" value="${e(n.date_from)}"></label><label>По дату включительно<input type="date" data-field="date_to" value="${e(n.date_to)}"></label><p class="hint">Проверяет текущее время и сразу выбирает Да/Нет, не ждёт открытия. 00:00–24:00 — весь день. 22:00–06:00 — ночная смена; день недели и даты относятся к началу смены. Праздники автоматически не учитываются.</p>`;
    if (n.type === 'subflow') html += '<p class="hint">Задайте название, например «Доставка». Соедините с её блоками, в конце поставьте «Возврат». Из основной цепочки используйте «Вызвать подцепочку», а не обычную прямую стрелку.</p>';
    if (n.type === 'return') html += '<p class="hint">Завершает подцепочку и продолжает цепочку вызывающего блока. Возвращаются только переменные, выбранные в блоке вызова.</p>';
    if (n.type === 'call_subflow') html += `<label>Подцепочка<select data-field="subflow_id"><option value="">Выберите вход…</option>${graph().nodes.filter(x => x.type === 'subflow').map(x => `<option value="${e(x.id)}" ${n.subflow_id === x.id ? 'selected' : ''}>${e(x.title)}</option>`).join('')}</select></label><label>Передать переменные<input data-field="pass_variables" value="${e(n.pass_variables.join(', '))}" placeholder="city, size"></label><label>Вернуть переменные<input data-field="return_variables" value="${e(n.return_variables.join(', '))}" placeholder="delivery_cost"></label><p class="hint">Имена через запятую. Имя клиента и последнее сообщение доступны всегда; остальные ответы передаются только по этому списку. Метки общие для клиента. Вложенность до 5, вызов по кругу запрещён. Подцепочки находятся в этом же сценарии.</p>`;
    if (["message", "question", "contact", "operator", "end", "wait_reply"].includes(n.type))
      html += `<label>Сообщение<textarea data-field="text" maxlength="3500" placeholder="Что скажет бот?">${e(n.text)}</textarea></label><div class="hint">Имя: <code>{first_name}</code>. Ответы клиента: <code>{answer}</code> или имя вашей переменной.</div>`;
    if (["message", "question", "contact", "random", "wait_reply"].includes(n.type))
      html += `<label class="upload-box">Вложение<input id="block-file" type="file" accept="image/*,video/*,audio/*,.pdf,.zip,.txt"><span class="hint">До 50 МБ. Видео и аудио отправляются как файлы.</span></label>${n.media_id ? `<div class="hint">${Admin.icon("paperclip")} ${e(media.find((m) => m.id === n.media_id)?.filename || "Файл")} <button class="btn ghost small" id="remove-media">Убрать</button></div>` : ""}`;
    if (['contact', 'set_variable', 'variable_condition', 'wait_reply'].includes(n.type)) {
      html += `<label>${n.type === 'variable_condition' ? 'Какую переменную проверить' : 'Имя переменной'}<input data-field="variable" value="${e(n.variable)}" pattern="[a-z][a-z0-9_]*" maxlength="32" placeholder="phone, email, interest"></label><p class="hint">Латинские буквы, цифры и _. В сообщении используйте <code>{${e(n.variable)}}</code>.</p>`;
    }
    if (n.type === 'contact') {
      html += `<label>Какой контакт запросить<select data-field="contact_type"><option value="phone" ${n.contact_type === 'phone' ? 'selected' : ''}>Телефон</option><option value="email" ${n.contact_type === 'email' ? 'selected' : ''}>Email</option></select></label><label class="check"><input type="checkbox" data-field="allow_skip" ${n.allow_skip ? 'checked' : ''}> Разрешить ответ «Пропустить»</label><label>Подсказка при неверном формате<textarea data-field="error_text" maxlength="500" placeholder="Пусто — стандартная подсказка бота">${e(n.error_text)}</textarea></label><p class="hint">Бот ждёт текстовый ответ и проверяет формат, но не принадлежность контакта. Телефон сразу попадает в карточку клиента; оба контакта сохраняются в ответах диалога. Это не подписка на рассылку.</p>`;
      html += `<div class="divider"></div><h3>Если контакт не оставлен</h3><label class="check"><input type="checkbox" data-field="reminder_enabled" ${n.reminder_enabled ? 'checked' : ''}> Напомнить, если человек молчит</label>${n.reminder_enabled ? contactReminderFields(n, 'reminder') : ''}<label class="check"><input type="checkbox" data-field="allow_later" ${n.allow_later ? 'checked' : ''}> Добавить кнопку «Позже»</label>`;
      if (n.allow_later) html += `<label>Ответ на «Позже»<textarea data-field="later_text" maxlength="3500" placeholder="Пусто — предложить прислать контакт в удобное время">${e(n.later_text)}</textarea></label><label class="check"><input type="checkbox" data-field="later_reminder_enabled" ${n.later_reminder_enabled ? 'checked' : ''}> Напомнить после «Позже»</label>${n.later_reminder_enabled ? contactReminderFields(n, 'later_reminder') : ''}`;
      html += '<p class="hint">Каждый таймер отправляет одно напоминание и оставляет сбор контакта открытым. Неверный ответ не меняет срок. «Позже» заменяет прежний таймер; повторное нажатие отсчитывает срок заново. Если напоминание после «Позже» выключено, старое отменяется.</p><p class="hint">Корректный контакт отменяет напоминание и ведёт дальше. «Пропустить» тоже ведёт дальше, но без нового контакта; «Позже» остаётся в этом блоке. Для обязательного контакта выключите «Пропустить». В текстах доступны переменные, например {first_name}.</p><p class="hint">Срок: от 1 секунды до 3650 дней. Таймер переживает перезапуск. «Стоп», запрет сообщений, менеджер, публикация новой версии или пауза отменяют напоминания. После простоя отправка возможна при запуске сервера, если контакт ещё не получен.</p>';
    }
    if (n.type === 'phone_condition') {
      html += `<label>Какой телефон учитывать<select data-field="phone_check_mode"><option value="provided" ${n.phone_check_mode === 'provided' ? 'selected' : ''}>Оставленный боту</option><option value="any" ${n.phone_check_mode === 'any' ? 'selected' : ''}>Любой сохранённый, включая VK</option></select></label><p class="hint">Проверяет карточку клиента в текущем проекте, даже после «Меню». Блок «Телефон / email» сохраняет телефон сразу. Email, произвольная переменная и текст последнего сообщения не считаются телефоном. Проверка не подтверждает принадлежность номера и не меняет согласие на рассылку.</p><p class="hint">Например: «Да» → продолжить, «Нет» → запросить телефон. Данные из VK заново не запрашиваются.</p>`;
    }
    if (n.type === 'set_variable') {
      html += `<label>Что записать<textarea data-field="value" maxlength="1000" placeholder="Например: доставка или Запрос: {answer}">${e(n.value)}</textarea></label><p class="hint">Без сообщения клиенту. Можно подставлять {first_name}, {answer} и другие ответы. Пустое значение очищает переменную; код и формулы не исполняются.</p>`;
    }
    if (n.type === 'variable_condition') {
      html += `<label>Условие<select data-field="comparison">${Object.entries(comparisonNames).map(([key, text]) => `<option value="${key}" ${n.comparison === key ? 'selected' : ''}>${text}</option>`).join('')}</select></label>`;
      if (!['empty', 'not_empty'].includes(n.comparison)) html += `<label>С чем сравнить<input data-field="value" value="${e(n.value)}" maxlength="1000" placeholder="Например: доставка или 1500"></label>`;
      html += '<p class="hint">Проверяет сохранённый ответ, а не последнее сообщение. Текст — без учёта регистра; для больше/меньше нужны числа. Неподходящее число или отсутствующий ответ ведут в «Нет».</p>';
    }
    if (n.type === 'random') {
      html += `<h3>Варианты ответа</h3><p class="hint">От 2 до 10 текстов. Бот отправит один, затем перейдёт дальше. Повторы между разными обращениями возможны. Поддерживаются {first_name} и ваши переменные.</p>${n.variants.map((value, i) => `<div class="mini-card"><label>Вариант ${i + 1}<textarea data-field="variants.${i}" maxlength="3500">${e(value)}</textarea></label><button class="btn ghost small" data-delete-variant="${i}" ${n.variants.length <= 2 ? 'disabled' : ''}>Убрать вариант</button></div>`).join('')}<button id="add-variant" class="btn secondary small" ${n.variants.length >= 10 ? 'disabled' : ''}>${Admin.icon('plus')} Добавить вариант</button>`;
    }
    if (n.type === "question") {
      html += `<label>Сохранить ответ как<input data-field="variable" value="${e(n.variable)}" pattern="[a-z][a-z0-9_]*" maxlength="32"></label><p class="hint">Например size → в сообщении используйте {size}.</p><div class="divider"></div><h3>Ветки по ответу</h3><p class="hint">Проверяются сверху вниз. Слова разделяйте запятыми.</p>`;
      html += n.rules
        .map(
          (r, i) =>
            `<div class="mini-card"><label>Ответ содержит<input data-field="rules.${i}.words" value="${e(r.words)}" placeholder="доставка, привезти"></label>${targetSelect(`rules.${i}.target`, "Перейти к", r.target)}<button class="btn ghost small" data-delete-rule="${i}">Убрать ветку</button></div>`,
        )
        .join("");
      html +=
        '<button id="add-rule" class="btn secondary small"><svg class="icon" aria-hidden="true" focusable="false"><use href="/static/icons.svg?v=1#plus"></use></svg> Ветка ответа</button>';
    }
    if (n.type === "message") {
      html +=
        '<div class="divider"></div><h3>Кнопки под сообщением</h3><p class="hint">До пяти кнопок. Переход ведёт к любому блоку, включая менеджера или промокод.</p>';
      html += n.buttons
        .map(
          (b, i) =>
            `<div class="mini-card"><label>Надпись<input data-field="buttons.${i}.label" maxlength="40" value="${e(b.label)}"></label><label>Действие<select data-field="buttons.${i}.kind"><option value="next" ${b.kind === "next" ? "selected" : ""}>Перейти к блоку</option><option value="link" ${b.kind === "link" ? "selected" : ""}>Открыть ссылку</option></select></label>${b.kind === "link" ? `<label>Ссылка<input type="url" data-field="buttons.${i}.url" value="${e(b.url)}" placeholder="https://…"></label>` : targetSelect(`buttons.${i}.target`, "Следующий блок", b.target)}${
              b.kind === "next"
                ? `<label>Цвет<select data-field="buttons.${i}.color">${[
                    ["primary", "Синий"],
                    ["secondary", "Серый"],
                    ["positive", "Зелёный"],
                    ["negative", "Красный"],
                  ]
                    .map(
                      ([v, l]) =>
                        `<option value="${v}" ${b.color === v ? "selected" : ""}>${l}</option>`,
                    )
                    .join("")}</select></label>`
                : ""
            }<button class="btn ghost small" data-delete-button="${i}">Убрать кнопку</button></div>`,
        )
        .join("");
      if (n.buttons.length < 5)
        html +=
          '<button id="add-button" class="btn secondary small"><svg class="icon" aria-hidden="true" focusable="false"><use href="/static/icons.svg?v=1#plus"></use></svg> Добавить кнопку</button>';
    }
    if (n.type === "condition") {
      html += `<label>Что проверить<select data-field="condition"><option value="contains" ${n.condition === "contains" ? "selected" : ""}>Сообщение содержит слова</option><option value="member" ${n.condition === "member" ? "selected" : ""}>Подписан на сообщество</option><option value="promo_sent" ${n.condition === "promo_sent" ? "selected" : ""}>Получал промокод кампании</option></select></label>`;
      if (n.condition === "contains")
        html += `<label>Слова через запятую<input data-field="words" value="${e(n.words)}" placeholder="цена, стоимость"></label>`;
      if (n.condition === "promo_sent") html += campaignSelect(n);
    }
    if (n.type === "promo") html += campaignSelect(n);
    if (n.type === "operator")
      html +=
        '<p class="hint">Бот приостановит ответы клиенту и уведомит всех менеджеров из раздела «Общение». Первый ответивший или нажавший «Взять в работу» станет ответственным. Переписка продолжается в VK.</p>';
    if (n.type === 'wait_reply') html += targetSelect('yes', 'Если ответил вовремя', n.yes) + targetSelect('no', 'Если время вышло', n.no);
    else if (["condition", "variable_condition", "schedule", "tag_condition", "phone_condition"].includes(n.type))
      html +=
        targetSelect("yes", "Если да", n.yes) +
        targetSelect("no", "Если нет", n.no);
    else if (
      !["operator", "end", "return"].includes(n.type) &&
      !(n.type === "message" && n.buttons.length)
    )
      html += targetSelect(
        "next",
        n.type === "call_subflow" ? "После возврата" : n.type === "question" ? "Любой другой ответ" : n.type === 'contact' ? 'После получения контакта или «Пропустить»' : "Следующий блок",
        n.next,
      );
    if (!['start', 'subflow', 'operator', 'end', 'return'].includes(n.type))
      html += '<p class="hint">Отдельный блок «Завершение» не обязателен: пустой переход заканчивает цепочку без дополнительного сообщения. Вопрос и сбор контакта сначала ждут ответ; ожидание — свой срок. Внутри подцепочки нужен «Возврат».</p>';
    if (n.type !== "start")
      html +=
        '<div class="inspector-footer button-row"><button id="duplicate-node" class="btn secondary small">Дублировать</button><button id="delete-node" class="btn danger small">Удалить блок</button></div>';
    $("block-inspector").innerHTML = html;
    $("block-inspector")
      .querySelectorAll("[data-field]")
      .forEach((input) => {
        input.addEventListener("focus", () => snapshot(), { once: true });
        input.addEventListener("input", () => {
          let value = input.type === 'checkbox' ? input.checked : input.value;
          if (input.dataset.field === "campaign_id")
            value = value ? Number(value) : null;
          if (input.dataset.field.endsWith('delay_value')) value = Number(value);
          if (['pass_variables', 'return_variables'].includes(input.dataset.field)) value = [...new Set(value.split(/[,\s]+/).filter(Boolean))];
          if (input.dataset.field === 'contact_type') {
            const previous = n.contact_type;
            if (n.variable === previous) n.variable = value;
            const prompts = { phone: 'Оставьте телефон для связи, например +7 999 123-45-67.', email: 'Оставьте email для связи, например name@example.com.' };
            if (n.text === prompts[previous]) n.text = prompts[value];
          }
          set(n, input.dataset.field, value);
          markDirty();
          renderNodes();
          if (
            ['reminder_enabled', 'allow_later', 'later_reminder_enabled'].includes(input.dataset.field) ||
            input.tagName === "SELECT" &&
            (['condition', 'comparison', 'contact_type'].includes(input.dataset.field) ||
              input.dataset.field.endsWith(".kind"))
          )
            renderInspector();
        });
      });
    $('block-inspector').querySelectorAll('[data-weekday]').forEach(input => {
      input.onchange = () => {
        snapshot();
        const days = new Set(n.weekdays), day = Number(input.dataset.weekday);
        input.checked ? days.add(day) : days.delete(day);
        n.weekdays = [...days].sort(); markDirty(); renderNodes();
      };
    });
    if ($("scenario-title"))
      $("scenario-title").oninput = (ev) => {
        flow.title = ev.target.value;
        markDirty();
      };
    $('block-inspector').querySelectorAll('[data-entry]').forEach(input => {
      input.addEventListener(input.tagName === 'SELECT' ? 'change' : 'input', () => {
        snapshot();
        graph().entry = {...entryDefaults, ...graph().entry, [input.dataset.entry]: input.value};
        markDirty();
        renderNodes();
        if (input.dataset.entry === 'mode') renderInspector();
      });
    });
    if ($("add-button"))
      $("add-button").onclick = () => {
        snapshot();
        n.buttons.push({
          label: "Новая кнопка",
          kind: "next",
          target: "",
          url: "",
          color: "primary",
        });
        changed();
      };
    if ($("add-rule"))
      $("add-rule").onclick = () => {
        if (n.rules.length >= 10) return;
        snapshot();
        n.rules.push({ words: "", target: "" });
        changed();
      };
    if ($('add-variant')) $('add-variant').onclick = () => {
      if (n.variants.length >= 10) return;
      snapshot(); n.variants.push(''); changed();
    };
    $('block-inspector').querySelectorAll('[data-delete-variant]').forEach(button => {
      button.onclick = () => {
        if (n.variants.length <= 2) return;
        snapshot(); n.variants.splice(Number(button.dataset.deleteVariant), 1); changed();
      };
    });
    $("block-inspector")
      .querySelectorAll("[data-delete-button]")
      .forEach(
        (b) =>
          (b.onclick = () => {
            snapshot();
            n.buttons.splice(Number(b.dataset.deleteButton), 1);
            changed();
          }),
      );
    $("block-inspector")
      .querySelectorAll("[data-delete-rule]")
      .forEach(
        (b) =>
          (b.onclick = () => {
            snapshot();
            n.rules.splice(Number(b.dataset.deleteRule), 1);
            changed();
          }),
      );
    if ($("block-file"))
      $("block-file").onchange = async (ev) => {
        const file = ev.target.files[0];
        if (!file) return;
        if (file.size > 50 * 1024 * 1024) {
          Admin.toast("Максимальный размер файла — 50 МБ.");
          return;
        }
        await action(async () => {
          const data = new FormData();
          data.append("file", file);
          const asset = await Admin.api("/media", "POST", data);
          snapshot();
          media.push(asset);
          n.media_id = asset.id;
          changed();
          Admin.toast("Файл прикреплён к блоку. Сохраните сценарий.");
        });
      };
    if ($("remove-media"))
      $("remove-media").onclick = () => {
        snapshot();
        n.media_id = "";
        changed();
      };
    if ($("delete-node"))
      $("delete-node").onclick = () => {
        if (!confirm("Удалить блок и связи с ним?")) return;
        snapshot();
        graph().nodes = graph().nodes.filter((x) => x.id !== n.id);
        graph().nodes.forEach((x) =>
          exits(x).forEach((o) => {
            if (o.target === n.id) set(x, o.key, "");
          }),
        );
        selected = graph().nodes[0]?.id;
        changed();
      };
    if ($("duplicate-node"))
      $("duplicate-node").onclick = () => {
        snapshot();
        const other = structuredClone(n);
        other.id = "n" + crypto.randomUUID().replaceAll("-", "");
        other.x += 45;
        other.y += 90;
        other.title += " (копия)";
        graph().nodes.push(other);
        selected = other.id;
        changed();
      };
  }
  function changed() {
    pending = null;
    markDirty();
    renderNodes();
    renderInspector();
  }
  function refreshPicker() {
    const select = $("flow-select");
    select.innerHTML = flows
      .map(
        (f) =>
          `<option value="${f.id}" ${flow?.id === f.id ? "selected" : ""}>${e(f.title)}${f.active ? (f.published_entry?.mode === 'keywords' ? ' · по фразам' : ' · по умолчанию') : ''}</option>`,
      )
      .join("");
  }
  function refreshStatus() {
    $("flow-status").textContent = flow.active
      ? "Опубликован · v" + flow.version
      : flow.has_published
        ? "Приостановлен"
        : "Черновик";
    $("flow-status").classList.toggle("live", flow.active);
    $("pause-flow").hidden = !flow.active;
    $("dirty-state").textContent = dirty
      ? "Есть изменения"
      : flow.has_published && flow.has_changes
        ? "Все изменения сохранены · черновик не опубликован"
        : "Все изменения сохранены";
    refreshPicker();
  }
  function selectFlow(id) {
    flow = structuredClone(flows.find((f) => f.id === Number(id)));
    flow.graph.entry = {...entryDefaults, ...flow.graph.entry};
    flow.graph.nodes = flow.graph.nodes.map(n => ({...structuredClone(blockDefaults), ...n}));
    selected = flow.graph.nodes.find((n) => n.type === "start")?.id;
    dirty = false;
    pending = null;
    history = [];
    errors("");
    $("editor").hidden = false;
    $("flow-empty").hidden = true;
    document.querySelector(".flow-toolbar").hidden = false;
    refreshStatus();
    renderNodes();
    renderInspector();
    setZoom(0.7);
    $("flow-canvas").scrollTo(0, 0);
  }
  async function create() {
    if (
      dirty &&
      !confirm(
        "Есть несохранённые изменения. Создать другой сценарий без их сохранения?",
      )
    )
      return;
    await action(async () => {
      const f = await Admin.api("/scenarios", "POST");
      flows.push(f);
      selectFlow(f.id);
      Admin.toast("Пример готов. Настройте блоки и проверьте диалог.");
    });
  }
  async function save() {
    if (!flow.title.trim())
      throw new Error("Укажите название сценария в настройках блока «Начало».");
    const result = await Admin.api(`/scenarios/${flow.id}`, "PUT", {
      title: flow.title,
      graph: graph(),
      revision: flow.revision,
    });
    flows = flows.map((f) => (f.id === result.id ? result : f));
    flow = structuredClone(result);
    dirty = false;
    errors("");
    refreshStatus();
    renderNodes();
    renderInspector();
    return result;
  }
  async function validate() {
    const result = await Admin.api("/scenarios/validate", "POST", graph());
    errors(result.errors.join("\n"));
    return !result.errors.length;
  }
  function setZoom(value) {
    zoom = Math.min(1.5, Math.max(0.3, value));
    $("canvas-world").style.zoom = zoom;
    $("zoom-label").textContent = Math.round(zoom * 100) + "%";
  }
  document.querySelectorAll("[data-add]").forEach(
    (b) =>
      (b.onclick = () => {
        if (!flow || busy) return;
        if (graph().nodes.length >= 100) {
          Admin.toast("В одном сценарии не больше 100 блоков.");
          return;
        }
        snapshot();
        const c = $("flow-canvas");
        const n = {
          ...structuredClone(blockDefaults),
          id: "n" + crypto.randomUUID().replaceAll("-", ""),
          type: b.dataset.add,
          title: kinds[b.dataset.add][0],
          x: Math.min(9400, c.scrollLeft / zoom + 80),
          y: Math.min(
            9400,
            c.scrollTop / zoom + 90 + (graph().nodes.length % 4) * 55,
          ),
          text: "",
          next: "",
          yes: "",
          no: "",
          buttons: [],
          rules: [],
          variable: "answer",
          condition: "contains",
          words: "",
          campaign_id: null,
          media_id: "",
          contact_type: 'phone',
          allow_skip: true,
          error_text: '',
          comparison: 'equals',
          value: '',
          variants: b.dataset.add === 'random' ? ['Спасибо за ваш интерес, {first_name}!', 'Рады помочь, {first_name}!'] : [],
          delay_value: 3,
          delay_unit: 'hours',
        };
        if (n.type === 'contact') {
          n.variable = 'phone';
          n.text = 'Оставьте телефон для связи, например +7 999 123-45-67.';
        }
        if (n.type === 'wait_reply') n.text = 'Подскажите, нужна ли помощь с выбором?';
        graph().nodes.push(n);
        selected = n.id;
        changed();
        if (narrowEditor.matches) collapseLibrary(true);
      }),
  );
  $("new-flow").onclick = create;
  $("empty-create").onclick = create;
  $("flow-select").onchange = (ev) => {
    if (dirty && !confirm("Переключиться без сохранения изменений?")) {
      ev.target.value = flow.id;
      return;
    }
    selectFlow(ev.target.value);
  };
  $("save-flow").onclick = () =>
    flow &&
    action(async () => {
      await save();
      renderInspector();
      Admin.toast("Черновик сохранён. Для клиентов его нужно опубликовать.");
    });
  $("validate-flow").onclick = () =>
    flow &&
    action(async () => {
      if (await validate())
        Admin.toast("Сценарий готов к проверке и публикации.");
    });
  $("publish-flow").onclick = () =>
    flow &&
    action(async () => {
      if (!(await validate())) return;
      const keywordEntry = graph().entry?.mode === 'keywords';
      const replacesDefault = !keywordEntry && flows.some(f => f.active && f.id !== flow.id && f.published_entry?.mode !== 'keywords');
      if ((flow.active || replacesDefault) && !confirm(`Опубликовать сценарий? Ожидания предыдущей версии этого сценария будут отменены.${replacesDefault ? ' Прежний сценарий по умолчанию будет приостановлен вместе с его ожиданиями.' : ''} Другие сценарии по фразам продолжат работать.`)) return;
      await save();
      const result = await Admin.api(`/scenarios/${flow.id}/publish`, "POST", {
        revision: flow.revision,
      });
      flows = (await Admin.api('/scenarios')).scenarios;
      flow = structuredClone(result);
      refreshStatus();
      renderNodes();
      renderInspector();
      Admin.toast(
        "Сценарий опубликован. Бот будет использовать его в личных сообщениях.",
      );
    });
  $("pause-flow").onclick = () =>
    flow &&
    action(async () => {
      if (!confirm('Приостановить сценарий? Его текущие ожидания будут отменены и не возобновятся после включения.')) return;
      const result = await Admin.api(`/scenarios/${flow.id}/pause`, "POST", {
        revision: flow.revision,
      });
      flows = flows.map((f) => (f.id === result.id ? result : f));
      flow.revision = result.revision;
      flow.active = false;
      refreshStatus();
      Admin.toast(
        "Сценарий приостановлен. Действует обычный ответ из раздела «Общение».",
      );
    });
  $("delete-flow").onclick = () => {
    if (!flow || !confirm(`Удалить сценарий «${flow.title}» в корзину? Он перестанет отвечать клиентам. Несохранённые изменения будут потеряны; сохранённую версию можно восстановить.`)) return;
    action(async () => {
      await Admin.api(`/scenarios/${flow.id}/delete`, 'POST', { revision: flow.revision });
      flows = flows.filter(item => item.id !== flow.id);
      flow = null; dirty = false; selected = null; previewState = {};
      if (flows.length) selectFlow(flows[0].id);
      else {
        toggleFullscreen(false);
        $('editor').hidden = true;
        $('flow-empty').hidden = false;
        document.querySelector('.flow-toolbar').hidden = true;
      }
      Admin.toast('Сценарий в корзине. Для возврата откройте раздел «Корзина».');
    });
  };
  $("zoom-in").onclick = () => setZoom(zoom + 0.1);
  $("zoom-out").onclick = () => setZoom(zoom - 0.1);
  $("zoom-reset").onclick = () => {
    setZoom(0.7);
    $("flow-canvas").scrollTo(0, 0);
  };
  $("flow-canvas").addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || ev.target.closest(".flow-node")) return;
    const canvas = $("flow-canvas"),
      sx = ev.clientX,
      sy = ev.clientY,
      ox = canvas.scrollLeft,
      oy = canvas.scrollTop;
    canvas.setPointerCapture(ev.pointerId);
    canvas.style.cursor = "grabbing";
    canvas.onpointermove = (move) => {
      canvas.scrollLeft = ox - (move.clientX - sx);
      canvas.scrollTop = oy - (move.clientY - sy);
    };
    canvas.onpointerup = canvas.onpointercancel = () => {
      canvas.onpointermove = null;
      canvas.style.cursor = "";
    };
  });
  $("flow-canvas").addEventListener(
    "wheel",
    (ev) => {
      if (ev.ctrlKey || ev.metaKey) {
        ev.preventDefault();
        setZoom(zoom + (ev.deltaY < 0 ? 0.05 : -0.05));
      }
    },
    { passive: false },
  );
  window.addEventListener("beforeunload", (ev) => {
    if (dirty) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      if ($('preview-dialog').open) return;
      if ($('flow-app').classList.contains('is-fullscreen')) toggleFullscreen(false);
      pending = null;
      if (flow) renderNodes();
    }
    if (
      (ev.ctrlKey || ev.metaKey) &&
      ev.key === "z" &&
      !["INPUT", "TEXTAREA"].includes(ev.target.tagName) &&
      history.length &&
      !busy
    ) {
      ev.preventDefault();
      flow.graph = JSON.parse(history.pop());
      changed();
    }
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") {
      ev.preventDefault();
      if (flow)
        action(async () => {
          await save();
          Admin.toast("Сохранено");
        });
    }
  });
  function toggleFullscreen(expanded) {
    $('flow-app').classList.toggle('is-fullscreen', expanded);
    document.body.classList.toggle('flow-fullscreen', expanded);
    document.querySelectorAll('.sidebar, .project-topbar, .skip-link').forEach(el => { el.inert = expanded; });
    const button = $('fullscreen-flow');
    button.setAttribute('aria-pressed', String(expanded));
    button.innerHTML = `${Admin.icon(expanded ? 'minimize' : 'maximize')} ${expanded ? 'Выйти · Esc' : 'На весь экран'}`;
    requestAnimationFrame(drawEdges);
    button.focus();
  }
  $('fullscreen-flow').onclick = () => toggleFullscreen(!$('flow-app').classList.contains('is-fullscreen'));
  function bubble(message, isUser = false, note = "") {
    const el = document.createElement("div");
    el.className = "bubble" + (isUser ? " user" : "");
    el.textContent = message;
    if (note) {
      const small = document.createElement("small");
      small.textContent = note;
      el.append(small);
    }
    $("preview-messages").append(el);
    $("preview-messages").scrollTop = $("preview-messages").scrollHeight;
  }
  let previewBusy = false;
  async function previewStep(text = "", payload = {}, restart = false, resumeWait = false) {
    if (previewBusy) return;
    previewBusy = true;
    const buttons = $("preview-buttons");
    $("preview-form").querySelector("button").disabled = true;
    buttons.inert = true;
    $("restart-preview").disabled = true;
    const controls = document.querySelectorAll('.preview-controls input, .preview-controls select, #preview-member');
    controls.forEach(input => { input.disabled = true; });
    try {
      const result = await Admin.api("/preview", "POST", {
        graph: graph(),
        state: previewState,
        text,
        payload,
        member: $("preview-member").checked,
        phone_status: $('preview-phone').value,
        restart,
        resume_wait: resumeWait,
        simulated_at: $('preview-clock').value ? new Date($('preview-clock').value + 'Z').toISOString() : null,
        tags: $('preview-tags').value.split(',').map(s => s.trim()).filter(Boolean),
      });
      previewState = result.state;
      if (previewState.clock) $('preview-clock').value = new Date(previewState.clock).toISOString().slice(0, 19);
      $('preview-state').textContent = `Метки: ${(previewState.tags || []).join(', ') || 'нет'} · Вложенность: ${(previewState.stack || []).length}`;
      $('preview-contact-state').textContent = `Телефон сейчас: ${{missing: 'не оставлен', provided: 'оставлен боту', profile: 'получен из VK'}[previewState.phone_status] || 'не оставлен'}`;
      for (const m of result.messages) {
        bubble(
          m.text,
          false,
          [m.file ? "Файл: " + m.file : "", m.note].filter(Boolean).join("\n"),
        );
        if (m.keyboard) {
          buttons.replaceChildren();
          for (const row of m.keyboard.buttons) {
            for (const b of row) {
              const a = b.action;
              if (a.type === "open_link") {
                const link = document.createElement("a");
                link.textContent = a.label + " ";
                link.insertAdjacentHTML("beforeend", Admin.icon("external-link"));
                link.href = a.link;
                link.target = "_blank";
                link.rel = "noopener";
                buttons.append(link);
              } else {
                const button = document.createElement("button");
                button.textContent = a.label;
                button.onclick = () => {
                  bubble(a.label, true);
                  previewStep(a.label, JSON.parse(a.payload));
                };
                buttons.append(button);
              }
            }
          }
        }
      }
      if (previewState.handoff || !previewState.node_id) buttons.replaceChildren();
      if (previewState.waiting) {
        const contactReminder = previewState.waiting.kind === 'contact';
        if (!contactReminder) buttons.replaceChildren();
        const hint = document.createElement('div');
        hint.className = 'hint';
        hint.textContent = contactReminder ? `Напоминание через ${previewState.waiting.seconds} сек. Контакт можно прислать сейчас или после напоминания.` : `Ожидание: ${previewState.waiting.seconds} сек. В VK продолжится автоматически; здесь можно пропустить паузу.`;
        const skip = document.createElement('button');
        skip.id = 'skip-preview-wait';
        skip.textContent = contactReminder ? 'Отправить напоминание сейчас' : previewState.waiting.kind === 'wait_reply' ? 'Ответ не получен — время вышло' : 'Пропустить ожидание';
        skip.onclick = () => {
          bubble('Время ожидания прошло', false, 'Только симуляция — реальных отправок нет.');
          previewStep('', {}, false, true);
        };
        buttons.append(hint, skip);
      }
    } catch (err) {
      bubble(err.message, false, "Не удалось выполнить этот шаг.");
    } finally {
      previewBusy = false;
      $("restart-preview").disabled = false;
      $("preview-form").querySelector("button").disabled = false;
      buttons.inert = false;
      controls.forEach(input => { input.disabled = false; });
    }
  }
  async function restartPreview() {
    if (previewBusy) return;
    previewState = {};
    $("preview-messages").replaceChildren();
    $("preview-buttons").replaceChildren();
    await previewStep("", {}, true);
  }
  $("preview-flow").onclick = () =>
    flow &&
    action(async () => {
      if (!(await validate())) return;
      $("preview-dialog").showModal();
      await restartPreview();
    });
  $("restart-preview").onclick = restartPreview;
  $("close-preview").onclick = () => $("preview-dialog").close();
  $("preview-form").onsubmit = (ev) => {
    ev.preventDefault();
    if (previewBusy) return;
    const text = $("preview-text").value.trim();
    if (!text) return;
    bubble(text, true);
    $("preview-text").value = "";
    previewStep(text);
  };
  action(async () => {
    const data = await Admin.api("/scenarios");
    flows = data.scenarios;
    campaigns = data.campaigns;
    media = data.media;
    if (flows.length)
      selectFlow(flows.find((f) => f.active)?.id || flows[0].id);
    else {
      $("editor").hidden = true;
      $("flow-empty").hidden = false;
      document.querySelector(".flow-toolbar").hidden = true;
    }
  });
})();
