(() => {
  const $ = (id) => document.getElementById(id),
    e = Admin.escape;
  const kinds = {
    start: ["Начало", "▷"],
    message: ["Сообщение", "▤"],
    question: ["Вопрос", "?"],
    condition: ["Условие", "◇"],
    promo: ["Промокод", "%"],
    operator: ["Менеджер", "♧"],
    end: ["Завершение", "✓"],
  };
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
    if (n.type === "condition")
      return [
        { key: "yes", label: "Да", target: n.yes },
        { key: "no", label: "Нет", target: n.no },
      ];
    if (["end", "operator"].includes(n.type)) return [];
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
  function renderNodes() {
    if (!flow) return;
    const width = Math.max(1800, ...graph().nodes.map((n) => n.x + 400)),
      height = Math.max(950, ...graph().nodes.map((n) => n.y + 500));
    $("canvas-world").style.width = width + "px";
    $("canvas-world").style.height = height + "px";
    $("flow-nodes").innerHTML = graph()
      .nodes.map(
        (n) =>
          `<article class="flow-node ${n.id === selected ? "selected" : ""}" data-id="${e(n.id)}" data-kind="${n.type}" style="left:${n.x}px;top:${n.y}px" tabindex="0" aria-label="${e(n.title)}"><button class="node-port input" aria-label="Соединить с ${e(n.title)}" data-input="${e(n.id)}"></button><div class="node-heading"><span class="node-symbol">${kinds[n.type][1]}</span><strong>${e(n.title)}</strong><small>${kinds[n.type][0]}</small></div><div class="node-content">${e(n.type === "condition" ? (n.condition === "member" ? "Подписан на сообщество?" : n.condition === "promo_sent" ? "Уже получал промокод?" : `Содержит: ${n.words || "укажите слова"}`) : n.type === "start" ? "Первое сообщение или команда «меню»" : n.type === "promo" ? campaigns.find((c) => c.id === n.campaign_id)?.title || "Выберите кампанию" : n.text || "Нажмите, чтобы настроить")}${n.media_id ? "\n▧ Прикреплён файл" : ""}</div>${exits(
            n,
          )
            .map(
              (o) =>
                `<div class="node-output"><span>${o.link ? "↗ " : ""}${e(o.label)}</span>${!o.link ? `<button class="node-port ${pending?.id === n.id && pending?.key === o.key ? "pending" : ""}" data-output="${e(o.key)}" aria-label="Переход ${e(o.label)}"></button>` : ""}</div>`,
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
    return `<label>${e(label)}<select data-field="${path}"><option value="">Выберите блок…</option>${graph()
      .nodes.map(
        (n) =>
          `<option value="${e(n.id)}" ${value === n.id ? "selected" : ""}>${e(n.title)} · ${kinds[n.type][0]}</option>`,
      )
      .join("")}</select></label>`;
  }
  function campaignSelect(n) {
    return `<label>Кампания<select data-field="campaign_id"><option value="">Выберите кампанию…</option>${campaigns.map((c) => `<option value="${c.id}" ${n.campaign_id === c.id ? "selected" : ""}>${e(c.title)}${c.enabled ? "" : " (выключена)"}</option>`).join("")}</select></label><p class="hint">Используются сообщение, файл и промокод выбранной кампании.</p>`;
  }
  function renderInspector() {
    if (!flow) return;
    const n = node();
    if (!n) {
      $("block-inspector").innerHTML =
        '<div class="empty"><h3>Настройки блока</h3><p>Выберите блок на схеме.</p></div>';
      return;
    }
    let html = `<span class="eyebrow">НАСТРОЙКИ БЛОКА</span><h3>${kinds[n.type][1]} ${kinds[n.type][0]}</h3><label>Название блока<input data-field="title" value="${e(n.title)}" maxlength="120"></label>`;
    if (n.type === "start")
      html += `<label>Название сценария<input id="scenario-title" value="${e(flow.title)}" maxlength="120"></label><p class="hint">Запускается на первое сообщение и команду «меню». Активен один входной сценарий.</p>`;
    if (["message", "question", "operator", "end"].includes(n.type))
      html += `<label>Сообщение<textarea data-field="text" maxlength="3500" placeholder="Что скажет бот?">${e(n.text)}</textarea></label><div class="hint">Имя: <code>{first_name}</code>. Ответы клиента: <code>{answer}</code> или имя вашей переменной.</div>`;
    if (["message", "question"].includes(n.type))
      html += `<label class="upload-box">Вложение<input id="block-file" type="file" accept="image/*,video/*,audio/*,.pdf,.zip,.txt"><span class="hint">До 50 МБ. Видео и аудио отправляются как файлы.</span></label>${n.media_id ? `<div class="hint">▧ ${e(media.find((m) => m.id === n.media_id)?.filename || "Файл")} <button class="btn ghost small" id="remove-media">Убрать</button></div>` : ""}`;
    if (n.type === "question") {
      html += `<label>Сохранить ответ как<input data-field="variable" value="${e(n.variable)}" pattern="[a-z][a-z0-9_]*" maxlength="32"></label><p class="hint">Например size → в сообщении используйте {size}.</p><div class="divider"></div><h3>Ветки по ответу</h3><p class="hint">Проверяются сверху вниз. Слова разделяйте запятыми.</p>`;
      html += n.rules
        .map(
          (r, i) =>
            `<div class="mini-card"><label>Ответ содержит<input data-field="rules.${i}.words" value="${e(r.words)}" placeholder="доставка, привезти"></label>${targetSelect(`rules.${i}.target`, "Перейти к", r.target)}<button class="btn ghost small" data-delete-rule="${i}">Убрать ветку</button></div>`,
        )
        .join("");
      html +=
        '<button id="add-rule" class="btn secondary small">＋ Ветка ответа</button>';
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
          '<button id="add-button" class="btn secondary small">＋ Добавить кнопку</button>';
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
        '<p class="hint">Бот приостановит ответы клиенту. Менеджер продолжает переписку в VK. Его ID задаётся в разделе «Общение».</p>';
    if (n.type === "condition")
      html +=
        targetSelect("yes", "Если да", n.yes) +
        targetSelect("no", "Если нет", n.no);
    else if (
      !["operator", "end"].includes(n.type) &&
      !(n.type === "message" && n.buttons.length)
    )
      html += targetSelect(
        "next",
        n.type === "question" ? "Любой другой ответ" : "Следующий блок",
        n.next,
      );
    if (n.type !== "start")
      html +=
        '<div class="inspector-footer button-row"><button id="duplicate-node" class="btn secondary small">Дублировать</button><button id="delete-node" class="btn danger small">Удалить блок</button></div>';
    $("block-inspector").innerHTML = html;
    $("block-inspector")
      .querySelectorAll("[data-field]")
      .forEach((input) => {
        input.addEventListener("focus", () => snapshot(), { once: true });
        input.addEventListener("input", () => {
          let value = input.value;
          if (input.dataset.field === "campaign_id")
            value = value ? Number(value) : null;
          set(n, input.dataset.field, value);
          markDirty();
          renderNodes();
          if (
            input.tagName === "SELECT" &&
            (input.dataset.field === "condition" ||
              input.dataset.field.endsWith(".kind"))
          )
            renderInspector();
        });
      });
    if ($("scenario-title"))
      $("scenario-title").oninput = (ev) => {
        flow.title = ev.target.value;
        markDirty();
      };
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
          `<option value="${f.id}" ${flow?.id === f.id ? "selected" : ""}>${e(f.title)}${f.active ? " · активен" : ""}</option>`,
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
        };
        graph().nodes.push(n);
        selected = n.id;
        changed();
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
      await save();
      const result = await Admin.api(`/scenarios/${flow.id}/publish`, "POST", {
        revision: flow.revision,
      });
      flows = flows.map((f) =>
        f.id === result.id ? result : { ...f, active: false },
      );
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
  async function previewStep(text = "", payload = {}, restart = false) {
    if (previewBusy) return;
    previewBusy = true;
    const buttons = $("preview-buttons");
    $("preview-form").querySelector("button").disabled = true;
    buttons.inert = true;
    $("restart-preview").disabled = true;
    try {
      const result = await Admin.api("/preview", "POST", {
        graph: graph(),
        state: previewState,
        text,
        payload,
        member: $("preview-member").checked,
        restart,
      });
      previewState = result.state;
      for (const m of result.messages) {
        bubble(
          m.text,
          false,
          [m.file ? "▧ " + m.file : "", m.note].filter(Boolean).join("\n"),
        );
        if (m.keyboard) {
          buttons.replaceChildren();
          for (const row of m.keyboard.buttons) {
            for (const b of row) {
              const a = b.action;
              if (a.type === "open_link") {
                const link = document.createElement("a");
                link.textContent = a.label + " ↗";
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
      if (previewState.handoff) buttons.replaceChildren();
    } catch (err) {
      bubble(err.message, false, "Не удалось выполнить этот шаг.");
    } finally {
      previewBusy = false;
      $("restart-preview").disabled = false;
      $("preview-form").querySelector("button").disabled = false;
      buttons.inert = false;
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
