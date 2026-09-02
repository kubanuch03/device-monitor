"use strict";

const els = {
  view: document.getElementById("view"),
  empty: document.getElementById("empty"),
  summary: document.getElementById("summary"),
  updated: document.getElementById("updated"),
  alert: document.getElementById("alert"),
  editor: document.getElementById("editor"),
  form: document.getElementById("editor-form"),
  formError: document.getElementById("form-error"),
  title: document.getElementById("editor-title"),
  siteOptions: document.getElementById("site-options"),
};

let state = { devices: [], sites: [], categories: [], updated_at: null, poll_interval: 20 };
let editingId = null;
let timer = null;

// Экран описывается адресом страницы, а не переменной: перезагрузка и ссылка
// в новой вкладке возвращают ровно туда, где был. Три уровня:
//   без параметров   - список точек
//   #site=X          - категории точки X
//   #site=X&cat=Y    - устройства категории Y
//   #site=X&cat=     - устройства без категории (параметр есть, но пустой)
function route() {
  const params = new URLSearchParams((location.hash || "").replace(/^#/, ""));
  return {
    site: params.get("site"),
    category: params.has("cat") ? params.get("cat") : null,
  };
}

function goTo(site, category) {
  if (!site) {
    location.hash = "";
    return;
  }
  const params = new URLSearchParams({ site });
  if (category !== null && category !== undefined) params.set("cat", category);
  location.hash = params.toString();
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function ago(iso) {
  if (!iso) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} ч`;
  return `${Math.round(hours / 24)} дн`;
}

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return `${n} ${one}`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${n} ${few}`;
  return `${n} ${many}`;
}

const UNCATEGORIZED = "Без категории";
const STATE_LABEL = { up: "на связи", down: "не отвечает", unknown: "не проверено" };

function devicesOf(site, category) {
  return state.devices.filter(
    (d) => d.site === site && (category === null || (d.category || "") === category)
  );
}

function siteNames() {
  return (state.sites || []).map((s) => s.name);
}

function siteInfo(name) {
  return (state.sites || []).find((s) => s.name === name) || { name, note: "" };
}

function categoriesOf(site) {
  return (state.categories || []).filter((c) => c.site === site);
}

function categoryInfo(site, name) {
  return categoriesOf(site).find((c) => c.name === name) || { site, name, note: "" };
}

function statusOf(items) {
  if (!items.length) return "unknown";
  if (items.some((d) => d.status === "down")) return "down";
  if (items.every((d) => d.status === "unknown")) return "unknown";
  return "up";
}

function countLabel(items) {
  if (!items.length) return "устройств пока нет";
  return plural(items.length, "устройство", "устройства", "устройств");
}

function badgeLabel(items) {
  const down = items.filter((d) => d.status === "down").length;
  if (down) return `не отвечают ${down}`;
  if (!items.length) return "пусто";
  if (items.every((d) => d.status === "unknown")) return "проверяются";
  return "все на связи";
}

function showAlert(text) {
  els.alert.textContent = text;
  els.alert.hidden = !text;
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await response.json(); } catch (e) { /* пустой ответ - нормально */ }
  if (!response.ok) throw new Error((body && body.detail) || `Ошибка ${response.status}`);
  return body;
}

// ---------------------------------------------------------------------------
// общие элементы
// ---------------------------------------------------------------------------

function crumb(text, onclick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "crumb";
  button.textContent = text;
  button.onclick = onclick;
  return button;
}

function screenHead(title, subtitle, note, action) {
  const head = document.createElement("div");
  head.className = "screen-head";
  const h = document.createElement("h1");
  h.textContent = title;
  const sub = document.createElement("p");
  sub.textContent = subtitle;
  head.append(h, sub);
  if (action) head.append(action);
  if (note) {
    const el = document.createElement("p");
    el.className = "site-note";
    el.textContent = note;
    head.append(el);
  }
  return head;
}

function groupTile({ title, items, note, onOpen, openLabel, tunnel }) {
  // Если туннель точки лёг, статусы устройств недостоверны (все down не потому
  // что упали, а потому что до них нет связи) — показываем это состояние точки,
  // а не мнимую массовую смерть.
  const tunnelDown = tunnel === "down";
  const status = tunnelDown ? "down" : statusOf(items);
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = `site-tile ${status}`;
  tile.onclick = onOpen;

  const head = document.createElement("div");
  head.className = "site-tile-head";
  const name = document.createElement("span");
  name.className = "site-tile-name";
  name.textContent = title;
  const badge = document.createElement("span");
  badge.className = `state ${status}`;
  badge.textContent = tunnelDown ? "нет связи с туннелем" : badgeLabel(items);
  head.append(name, badge);
  if (tunnel && !tunnelDown) {
    const t = document.createElement("span");
    t.className = "tunnel-tag";
    t.textContent = "через туннель";
    t.title = "Проверка идёт через SOCKS-туннель объекта";
    name.after(t);
  }

  const count = document.createElement("div");
  count.className = "site-tile-count";
  count.textContent = countLabel(items);

  const dots = document.createElement("div");
  dots.className = "site-dots";
  items.slice(0, 24).forEach((device) => {
    const dot = document.createElement("span");
    dot.className = `site-dot ${device.status}`;
    dot.title = `${device.name} — ${STATE_LABEL[device.status]}`;
    dots.append(dot);
  });

  tile.append(head, count);
  if (note) {
    const el = document.createElement("div");
    el.className = "site-note";
    el.textContent = note;
    tile.append(el);
  }
  const go = document.createElement("span");
  go.className = "site-tile-go";
  go.textContent = openLabel;
  tile.append(dots, go);
  return tile;
}

function newTile(label, onclick) {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "site-tile new";
  tile.onclick = onclick;
  const plus = document.createElement("span");
  plus.className = "plus";
  plus.textContent = "+";
  const text = document.createElement("span");
  text.className = "label";
  text.textContent = label;
  tile.append(plus, text);
  return tile;
}

// ---------------------------------------------------------------------------
// карточка устройства
// ---------------------------------------------------------------------------

function deviceCard(device) {
  const card = document.createElement("article");
  card.className = `card ${device.status}`;

  const head = document.createElement("div");
  head.className = "card-head";
  const name = document.createElement("span");
  name.className = "card-name";
  name.textContent = device.name;
  const badge = document.createElement("span");
  badge.className = `state ${device.status}`;
  badge.textContent = STATE_LABEL[device.status] || STATE_LABEL.unknown;
  head.append(name, badge);

  const host = document.createElement("div");
  host.className = "card-host";
  host.textContent = device.url;

  const ports = document.createElement("div");
  ports.className = "ports";
  device.ports.forEach((port) => {
    const chip = document.createElement("span");
    const open = device.ports_state ? device.ports_state[String(port)] : undefined;
    chip.className = "port" + (open === true ? " open" : open === false ? " closed" : "");
    chip.textContent = port;
    chip.title = open === true ? "порт открыт" : open === false ? "порт закрыт" : "не проверялся";
    ports.append(chip);
  });

  const meta = document.createElement("div");
  meta.className = "card-meta";
  const bits = [];
  if (device.status === "up" && device.latency_ms != null) bits.push(`${device.latency_ms} мс`);
  if (device.since) bits.push(`в этом состоянии ${ago(device.since)}`);
  meta.textContent = bits.join(" · ") || "ожидает первой проверки";

  const actions = document.createElement("div");
  actions.className = "card-actions";

  const open = document.createElement("button");
  open.className = "primary";
  open.type = "button";
  open.textContent = "Открыть";
  // Прямая точка — обычная вкладка (браузер дойдёт сам). Точка за прокси —
  // через devmon://, чтобы локальный лаунчер поднял профиль этой точки с её
  // SOCKS: иначе обычная вкладка до устройства за туннелем не достучится.
  const siteProxy = (siteInfo(device.site) || {}).proxy;
  if (siteProxy) {
    open.onclick = () => {
      const link = "devmon://open?site=" + encodeURIComponent(device.site)
        + "&proxy=" + encodeURIComponent(siteProxy)
        + "&url=" + encodeURIComponent(device.url);
      window.location.href = link;
    };
    open.title = "Откроется в профиле точки через её туннель (нужен установленный обработчик)";
  } else {
    open.onclick = () => window.open(device.url, "_blank", "noopener");
  }

  const info = document.createElement("button");
  info.type = "button";
  info.textContent = "Сведения";
  info.onclick = () => openDetail(device.id);

  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "Настроить";
  edit.onclick = () => openEditor(device);

  // Дублирование открывает форму с заполненными полями, а не создаёт копию
  // сразу: смысл операции - завести соседнее устройство с той же настройкой,
  // но другим адресом, и адрес всё равно придётся менять перед сохранением.
  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "Дублировать";
  copy.onclick = () =>
    openEditor({ ...device, id: null, name: `${device.name} (копия)` }, null, true);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "subtle danger";
  remove.textContent = "Удалить";
  remove.onclick = async () => {
    if (!confirm(`Удалить «${device.name}» из списка?`)) return;
    try {
      await api(`/api/devices/${device.id}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      showAlert(error.message);
    }
  };

  actions.append(open, info, edit, copy, remove);
  card.append(head, host, ports, meta);

  if (device.note) {
    const note = document.createElement("div");
    note.className = "card-note";
    note.textContent = device.note;
    card.append(note);
  }

  card.append(actions);
  return card;
}

// ---------------------------------------------------------------------------
// экран 1: точки
// ---------------------------------------------------------------------------

function renderSitesScreen() {
  els.view.append(screenHead("Точки", "Выберите точку, чтобы увидеть её оборудование."));

  const grid = document.createElement("div");
  grid.className = "site-grid";
  siteNames().forEach((site) => {
    const meta = siteInfo(site);
    grid.append(
      groupTile({
        title: site,
        items: devicesOf(site, null),
        note: meta.note,
        tunnel: meta.proxy ? (meta.tunnel || "unknown") : null,
        openLabel: "Открыть точку →",
        onOpen: () => goTo(site, null),
      })
    );
  });
  grid.append(newTile("Новая точка", () => openGroupEditor("site", null)));
  els.view.append(grid);
}

// ---------------------------------------------------------------------------
// экран 2: категории точки
// ---------------------------------------------------------------------------

function renderCategoriesScreen(site) {
  const all = devicesOf(site, null);
  const settings = document.createElement("button");
  settings.type = "button";
  settings.textContent = "Настроить точку";
  settings.onclick = () => openGroupEditor("site", site);

  const meta = siteInfo(site);
  els.view.append(
    crumb("← Все точки", () => goTo(null)),
    screenHead(site, `${countLabel(all)} · ${badgeLabel(all)}`, meta.note, settings)
  );
  if (meta.proxy && meta.tunnel === "down") {
    const warn = document.createElement("div");
    warn.className = "alert";
    warn.textContent = "Нет связи с туннелем объекта — статусы устройств недостоверны. "
      + (meta.tunnel_detail || "Проверьте, что SSH-туннель поднят.");
    els.view.append(warn);
  }

  const grid = document.createElement("div");
  grid.className = "site-grid";

  categoriesOf(site).forEach((category) => {
    grid.append(
      groupTile({
        title: category.name,
        items: devicesOf(site, category.name),
        note: category.note,
        openLabel: "Открыть →",
        onOpen: () => goTo(site, category.name),
      })
    );
  });

  // Устройства без категории показываются отдельной плиткой и только когда
  // они есть: иначе это была бы пустая ячейка на каждом экране.
  const loose = devicesOf(site, "");
  if (loose.length) {
    grid.append(
      groupTile({
        title: UNCATEGORIZED,
        items: loose,
        openLabel: "Открыть →",
        onOpen: () => goTo(site, ""),
      })
    );
  }

  grid.append(newTile("Новая категория", () => openGroupEditor("category", null, site)));
  els.view.append(grid);
}

// ---------------------------------------------------------------------------
// экран 3: устройства категории
// ---------------------------------------------------------------------------

function renderDevicesScreen(site, category) {
  const items = devicesOf(site, category).sort((a, b) => a.name.localeCompare(b.name, "ru"));
  const title = category || UNCATEGORIZED;

  // У псевдокатегории «Без категории» нечего настраивать: это не запись,
  // а способ показать то, что ещё не разложено.
  let settings = null;
  if (category) {
    settings = document.createElement("button");
    settings.type = "button";
    settings.textContent = "Настроить категорию";
    settings.onclick = () => openGroupEditor("category", category, site);
  }

  els.view.append(
    crumb(`← ${site}`, () => goTo(site, null)),
    screenHead(
      title,
      `${countLabel(items)} · ${badgeLabel(items)}`,
      category ? categoryInfo(site, category).note : "",
      settings
    )
  );

  if (items.length) {
    const grid = document.createElement("div");
    grid.className = "grid";
    items.forEach((device) => grid.append(deviceCard(device)));
    els.view.append(grid);
  } else {
    const empty = document.createElement("div");
    empty.className = "site-empty";
    empty.textContent = "В этой категории пока нет устройств.";
    els.view.append(empty);
  }

  const add = document.createElement("button");
  add.type = "button";
  add.className = "add-here";
  add.textContent = "+ Добавить устройство сюда";
  add.onclick = () => openEditor(null, { site, category: category || "" });
  els.view.append(add);
}

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------

function render() {
  els.view.textContent = "";
  const devices = state.devices || [];
  const hasAnything = devices.length > 0 || (state.sites || []).length > 0;
  els.empty.hidden = hasAnything;

  const { site, category } = route();

  // Точка или категория могли исчезнуть, пока экран был открыт. Молча
  // поднимаемся на уровень выше вместо экрана несуществующего раздела.
  const knownSites = siteNames();
  if (site && !knownSites.includes(site)) {
    goTo(null);
    return;
  }
  if (site && category) {
    const known = categoriesOf(site).map((c) => c.name);
    if (!known.includes(category)) {
      goTo(site, null);
      return;
    }
  }

  const scope = site ? devicesOf(site, category) : devices;
  const up = scope.filter((d) => d.status === "up").length;
  const down = scope.filter((d) => d.status === "down").length;
  els.summary.innerHTML = scope.length
    ? `<span>${site ? "здесь" : "устройств"} <b>${scope.length}</b></span>` +
      `<span class="s-up">на связи <b>${up}</b></span>` +
      `<span class="s-down">не отвечают <b>${down}</b></span>`
    : "";
  els.updated.textContent = state.updated_at ? `проверено ${ago(state.updated_at)} назад` : "";

  if (hasAnything) {
    if (site && category !== null) renderDevicesScreen(site, category);
    else if (site) renderCategoriesScreen(site);
    else renderSitesScreen();
  }

  els.siteOptions.textContent = "";
  knownSites.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    els.siteOptions.append(option);
  });

  const categoryOptions = document.getElementById("category-options");
  const builtin = ["Камеры", "Домофоны", "Шлагбаумы", "Терминалы", "Коммутаторы", "Серверы"];
  const all = [...new Set([...(state.categories || []).map((c) => c.name), ...builtin])].sort(
    (a, b) => a.localeCompare(b, "ru")
  );
  categoryOptions.textContent = "";
  all.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    categoryOptions.append(option);
  });
}

// ---------------------------------------------------------------------------
// данные
// ---------------------------------------------------------------------------

async function refresh() {
  try {
    state = await api("/api/state");
    showAlert("");
    render();
    schedule();
  } catch (error) {
    showAlert(`Сервис не отвечает: ${error.message}. Проверяю снова через 10 секунд.`);
    clearTimeout(timer);
    timer = setTimeout(refresh, 10000);
  }
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(refresh, Math.max(5, state.poll_interval || 20) * 1000);
}

window.addEventListener("hashchange", render);

// ---------------------------------------------------------------------------
// редактор точки и категории
// ---------------------------------------------------------------------------

const groupEditor = document.getElementById("group-editor");
const groupForm = document.getElementById("group-form");
const groupError = document.getElementById("group-form-error");
const groupDelete = document.getElementById("group-delete");
let groupKind = "site";
let groupOriginal = null;
let groupSite = null;

function openGroupEditor(kind, name, site) {
  groupKind = kind;
  groupOriginal = name;
  groupSite = site || null;

  const isSite = kind === "site";
  document.getElementById("group-editor-title").textContent = name
    ? isSite ? "Настройки точки" : "Настройки категории"
    : isSite ? "Новая точка" : "Новая категория";
  document.getElementById("g-name-label").textContent = isSite
    ? "Название точки"
    : "Название категории";
  groupForm.name.placeholder = isSite ? "ТЦ Ала-Арча" : "Камеры";
  groupForm.note.placeholder = isSite ? "Адрес, подсеть, кто отвечает" : "Что сюда относится";

  const info = name
    ? isSite ? siteInfo(name) : categoryInfo(site, name)
    : { name: "", note: "", proxy: "" };
  groupForm.name.value = info.name;
  groupForm.note.value = info.note || "";
  // Прокси — только у точки; для категории поле прячем.
  document.getElementById("g-proxy-field").hidden = !isSite;
  groupForm.proxy.value = isSite ? (info.proxy || "") : "";
  groupDelete.hidden = !name;
  groupDelete.textContent = isSite ? "Удалить точку" : "Удалить категорию";
  groupError.hidden = true;
  groupEditor.showModal();
  groupForm.name.focus();
}

groupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const isSite = groupKind === "site";
  const payload = { name: groupForm.name.value, note: groupForm.note.value };
  if (isSite) payload.proxy = groupForm.proxy.value;
  try {
    let result;
    if (isSite) {
      result = groupOriginal
        ? await api(`/api/sites/${encodeURIComponent(groupOriginal)}`, {
            method: "PUT", body: JSON.stringify(payload),
          })
        : await api("/api/sites", { method: "POST", body: JSON.stringify(payload) });
    } else {
      result = groupOriginal
        ? await api(
            `/api/categories/${encodeURIComponent(groupSite)}/${encodeURIComponent(groupOriginal)}`,
            { method: "PUT", body: JSON.stringify(payload) }
          )
        : await api("/api/categories", {
            method: "POST", body: JSON.stringify({ ...payload, site: groupSite }),
          });
    }
    groupEditor.close();
    await refresh();
    // После переименования адрес всё ещё указывает на старое имя, а после
    // создания логично сразу открыть то, что создали.
    if (isSite) goTo(result.site.name, null);
    else goTo(result.category.site, result.category.name);
    render();
  } catch (error) {
    groupError.textContent = error.message;
    groupError.hidden = false;
  }
});

groupDelete.onclick = async () => {
  if (!groupOriginal) return;
  const isSite = groupKind === "site";
  if (!confirm(`Удалить ${isSite ? "точку" : "категорию"} «${groupOriginal}»?`)) return;
  try {
    await api(
      isSite
        ? `/api/sites/${encodeURIComponent(groupOriginal)}`
        : `/api/categories/${encodeURIComponent(groupSite)}/${encodeURIComponent(groupOriginal)}`,
      { method: "DELETE" }
    );
    groupEditor.close();
    if (isSite) goTo(null);
    else goTo(groupSite, null);
    await refresh();
  } catch (error) {
    groupError.textContent = error.message;
    groupError.hidden = false;
  }
};

document.getElementById("group-cancel").onclick = () => groupEditor.close();

// ---------------------------------------------------------------------------
// редактор устройства
// ---------------------------------------------------------------------------

function openEditor(device, preset, asCopy) {
  editingId = asCopy ? null : device ? device.id : null;
  els.title.textContent = asCopy
    ? "Копия устройства"
    : device ? "Настройки устройства" : "Новое устройство";

  const here = route();
  els.form.site.value = device ? device.site : (preset && preset.site) || here.site || "";
  els.form.category.value = device
    ? device.category || ""
    : (preset && preset.category) || here.category || "";
  els.form.name.value = device ? device.name : "";
  els.form.host.value = device ? device.host : "";
  els.form.scheme.value = device ? device.scheme : "http";
  els.form.ports.value = device ? device.ports.join(", ") : "80";
  els.form.web_port.value = device && device.web_port ? device.web_port : "";
  els.form.path.value = device ? device.path || "/" : "";
  els.form.note.value = device ? device.note || "" : "";
  els.formError.hidden = true;
  els.editor.showModal();
  // У копии первым делом меняют адрес - на нём и ставим курсор.
  (device && !asCopy ? els.form.name : els.form.host).focus();
  if (asCopy) els.form.host.select();
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    site: els.form.site.value,
    category: els.form.category.value,
    name: els.form.name.value,
    host: els.form.host.value,
    scheme: els.form.scheme.value,
    ports: els.form.ports.value,
    web_port: els.form.web_port.value,
    path: els.form.path.value,
    note: els.form.note.value,
  };
  try {
    const result = editingId
      ? await api(`/api/devices/${editingId}`, { method: "PUT", body: JSON.stringify(payload) })
      : await api("/api/devices", { method: "POST", body: JSON.stringify(payload) });
    els.editor.close();
    await refresh();
    // Устройство могло уехать в другую точку или категорию - показываем там,
    // иначе оно «пропадает» с текущего экрана без объяснений.
    if (result && result.device) {
      goTo(result.device.site, result.device.category || "");
      render();
    }
  } catch (error) {
    els.formError.textContent = error.message;
    els.formError.hidden = false;
  }
});

document.getElementById("editor-cancel").onclick = () => els.editor.close();

// ---------------------------------------------------------------------------
// карточка сведений: что устройство рассказало о себе + история доступности
// ---------------------------------------------------------------------------

const detail = document.getElementById("detail");
const detailBody = document.getElementById("detail-body");
let detailId = null;

const FACT_LABELS = {
  vendor: "Производитель",
  model: "Модель",
  serial: "Серийный номер",
  firmware: "Прошивка",
  released: "Дата прошивки",
  hardware: "Аппаратная версия",
  mac: "MAC-адрес",
  device_name: "Имя в устройстве",
  fingerprint: "Отпечаток (не меняется при смене IP)",
  detail: "Примечание",
};
const FACT_ORDER = ["vendor", "model", "serial", "firmware", "released", "hardware", "mac",
                    "device_name", "fingerprint", "detail"];

function row(label, value) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  return [dt, dd];
}

function mkBtn(text, onclick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "cred-btn";
  b.textContent = text;
  b.onclick = onclick;
  return b;
}

async function copyText(text, button) {
  const original = button.textContent;
  try {
    // clipboard API работает в защищённом контексте, а 127.0.0.1 таковым
    // считается; execCommand - запасной путь для старых webview.
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.append(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    button.textContent = "Скопировано";
  } catch (e) {
    button.textContent = "Не вышло";
  }
  setTimeout(() => { button.textContent = original; }, 1500);
}

function credRow(label, shownValue, copyValue) {
  const row = document.createElement("div");
  row.className = "cred-row";
  const lab = document.createElement("span");
  lab.className = "cred-label";
  lab.textContent = label;
  const val = document.createElement("code");
  val.className = "cred-value";
  val.textContent = shownValue;
  row.append(lab, val);
  if (copyValue) {
    row.append(mkBtn("Копировать", (e) => copyText(copyValue, e.currentTarget)));
  }
  return row;
}

async function openDetail(id) {
  detailId = id;
  detailBody.textContent = "Загружаю…";
  detail.showModal();
  await renderDetail();
}

async function renderDetail() {
  const device = (state.devices || []).find((d) => d.id === detailId);
  if (!device) { detail.close(); return; }

  detailBody.textContent = "";
  const title = document.createElement("h2");
  title.textContent = device.name;
  const sub = document.createElement("p");
  sub.className = "detail-sub";
  sub.textContent = `${device.site} · ${device.category || "Без категории"} · ${device.url}`;
  detailBody.append(title, sub);

  const facts = device.facts || {};
  const dl = document.createElement("dl");
  dl.className = "facts-list";
  FACT_ORDER.forEach((key) => {
    if (facts[key]) dl.append(...row(FACT_LABELS[key], facts[key]));
  });
  if (!dl.children.length) {
    const note = document.createElement("p");
    note.className = "detail-sub";
    note.textContent = "Сведения ещё не собраны — нажмите «Опросить устройство».";
    detailBody.append(note);
  } else {
    detailBody.append(dl);
  }
  if (!facts.serial && device.has_password === false && facts.vendor) {
    const hint = document.createElement("p");
    hint.className = "detail-hint";
    hint.textContent = "Модель, серийник и прошивку устройство отдаёт только по логину — "
      + "добавьте учётные данные в «Настроить».";
    detailBody.append(hint);
  }

  // Учётные данные: логин копируется свободно, пароль - по кнопке, отдельным
  // запросом, чтобы секрет не тянулся в общий стейт.
  if (device.username || device.has_password) {
    const h = document.createElement("h3");
    h.textContent = "Учётные данные";
    const box = document.createElement("div");
    box.className = "creds";

    box.append(credRow("Логин", device.username || "—", device.username || ""));

    const passRow = document.createElement("div");
    passRow.className = "cred-row";
    const label = document.createElement("span");
    label.className = "cred-label";
    label.textContent = "Пароль";
    const value = document.createElement("code");
    value.className = "cred-value";
    value.textContent = device.has_password ? "••••••••" : "не задан";
    passRow.append(label, value);

    if (device.has_password) {
      let shown = false;
      let secret = "";
      const ensure = async () => {
        if (!secret) {
          const data = await api(`/api/devices/${device.id}/secret`);
          secret = data.password || "";
        }
        return secret;
      };
      const show = mkBtn("Показать", async () => {
        shown = !shown;
        value.textContent = shown ? (await ensure()) || "—" : "••••••••";
        show.textContent = shown ? "Скрыть" : "Показать";
      });
      const copy = mkBtn("Копировать", async () => {
        await copyText(await ensure(), copy);
      });
      passRow.append(show, copy);
    }
    box.append(passRow);
    detailBody.append(h, box);
  }

  // История доступности
  try {
    const data = await api(`/api/devices/${device.id}/history`);
    const up = data.uptime || {};
    const stat = document.createElement("div");
    stat.className = "uptime";
    const pct = up.uptime == null ? "—" : `${up.uptime}%`;
    stat.innerHTML =
      `<span>аптайм за ${up.days} дн: <b>${pct}</b></span>` +
      `<span>падений: <b>${up.outages ?? 0}</b></span>`;
    detailBody.append(stat);

    if ((data.history || []).length) {
      const h = document.createElement("h3");
      h.textContent = "История состояний";
      const list = document.createElement("div");
      list.className = "history";
      data.history.forEach((event) => {
        const line = document.createElement("div");
        line.className = "history-row";
        const dot = document.createElement("span");
        dot.className = `dot ${event.status}`;
        const label = document.createElement("span");
        label.textContent = event.status === "up" ? "на связи" : "не отвечает";
        const when = document.createElement("span");
        when.className = "history-time";
        when.textContent = new Date(event.at).toLocaleString("ru-RU");
        line.append(dot, label, when);
        list.append(line);
      });
      detailBody.append(h, list);
    }
  } catch (e) { /* история необязательна */ }
}

document.getElementById("detail-close").onclick = () => detail.close();
document.getElementById("detail-inspect").onclick = async (event) => {
  if (!detailId) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Опрашиваю…";
  try {
    await api(`/api/devices/${detailId}/inspect`, { method: "POST" });
    // Опрос сведений идёт в фоне и занимает несколько секунд - ждём круг,
    // затем перечитываем состояние и перерисовываем карточку.
    setTimeout(async () => { await refresh(); await renderDetail(); }, 2500);
  } catch (error) {
    showAlert(error.message);
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = "Опросить устройство"; }, 2500);
  }
};

document.getElementById("add").onclick = () => openEditor(null);
document.getElementById("add-first").onclick = () => openEditor(null);

document.getElementById("recheck").onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Проверяю…";
  try {
    await api("/api/recheck", { method: "POST" });
    // Опрос будится сразу, но самим проверкам нужно время на таймауты
    // мёртвых адресов - иначе перерисуем ещё старое состояние.
    setTimeout(refresh, 1500);
  } catch (error) {
    showAlert(error.message);
  } finally {
    setTimeout(() => {
      button.disabled = false;
      button.textContent = "Проверить сейчас";
    }, 1500);
  }
};

refresh();
