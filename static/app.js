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
// Вид главного экрана. По умолчанию топология; явный выбор пользователя
// сохраняется и переживает перезагрузку. Значение "list" осмысленное, а не
// «не задано», поэтому проверяем именно его, а не truthy.
const VIEW_KEY = "dm_view_mode";
let topologyView = localStorage.getItem(VIEW_KEY) !== "list";

// Экран описывается адресом страницы, а не переменной: перезагрузка и ссылка
// в новой вкладке возвращают ровно туда, где был. Три уровня:
//   без параметров   - список точек
//   #site=X          - категории точки X
//   #site=X&cat=Y    - устройства категории Y
//   #site=X&cat=     - устройства без категории (параметр есть, но пустой)
//   #q=текст         - поиск по всем точкам
//   #down=1          - все, кто не отвечает, по всем точкам
function route() {
  const params = new URLSearchParams((location.hash || "").replace(/^#/, ""));
  return {
    site: params.get("site"),
    category: params.has("cat") ? params.get("cat") : null,
    query: (params.get("q") || "").trim(),
    onlyDown: params.has("down"),
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

// Схемы авторизации, которые браузер отрабатывает сам по адресу с учёткой.
// Заполняется на сервере пробником probing/webauth.py: он спрашивает ровно тот
// адрес, который откроется кнопкой, и смотрит WWW-Authenticate в ответе 401.
const AUTOLOGIN_SCHEMES = ["basic", "digest"];

function webAuthOf(device) {
  return ((device.facts || {}).web_auth || "").toLowerCase();
}

/** Адрес с учёткой внутри: http://логин:пароль@хост/путь */
function withCredentials(rawUrl, username, password) {
  const url = new URL(rawUrl, window.location.href);
  // Строку собираем сами, а не через сеттеры url.username/url.password:
  // они кодируют значение по своим правилам, и пароль, в котором уже есть
  // процент или двоеточие, приехал бы на устройство другим паролем.
  const creds = `${encodeURIComponent(username)}:${encodeURIComponent(password)}`;
  return `${url.protocol}//${creds}@${url.host}${url.pathname}${url.search}${url.hash}`;
}

/**
 * Открыть веб-интерфейс устройства, подставив учётку, если это возможно.
 *
 * Вкладка открывается СРАЗУ, до запроса секрета, и только потом получает
 * адрес. Наоборот нельзя: после await браузер уже не считает открытие
 * следствием клика и глушит вкладку как всплывающее окно.
 */
async function openDevice(device, target, autoLogin, button) {
  if (!device.has_password) {
    window.open(target, "_blank", "noopener");
    return;
  }
  if (!autoLogin) {
    // Пароль формой — открываем как обычно и кладём пароль в буфер.
    window.open(target, "_blank", "noopener");
    await copyDevicePassword(device, button);
    return;
  }
  // noopener здесь не поставить: с ним window.open возвращает null и адрес
  // вкладке уже не задать. Вместо флага рвём связь вручную, пока вкладка ещё
  // пустая и своя, — иначе морда устройства смогла бы увести панель на себя.
  const tab = window.open("", "_blank");
  if (!tab) {
    // Вкладку зарезал блокировщик всплывающих окон. Молча открыть без учётки
    // нельзя - человек решит, что автоподстановка сломалась, и пойдёт искать
    // причину в устройстве.
    showAlert("Браузер заблокировал новую вкладку. Разрешите всплывающие окна "
              + "для этой страницы — без вкладки подставить учётку некуда.");
    return;
  }
  try { tab.opener = null; } catch (e) { /* уже отвязана */ }
  let secret;
  try {
    secret = await api(`/api/v1/devices/${device.id}/secret`);
  } catch (error) {
    showAlert(error.message);
    tab.location = target;   // без учётки, но открыть всё равно надо
    return;
  }
  tab.location = withCredentials(target, secret.username, secret.password);
}

async function copyDevicePassword(device, button) {
  try {
    const secret = await api(`/api/v1/devices/${device.id}/secret`);
    if (!secret.password) return;
    await copyText(secret.password, button);
  } catch (error) {
    showAlert(error.message);
  }
}

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
  // Сессии живут в памяти сервиса: после его перезапуска вход слетает, и любой
  // запрос отвечает 401. Раньше ошибка тихо уходила в баннер под открытым
  // диалогом (например, «глаз» пароля просто ничего не делал). Теперь - на вход.
  if (response.status === 401 && !path.startsWith("/api/v1/login")) {
    window.location.href = "/";
    throw new Error("Сессия истекла - нужен вход");
  }
  if (!response.ok) throw new Error((body && body.detail) || `Ошибка ${response.status}`);
  return body;
}

// «Не тревожить»: выпадающий список рядом с устройством или точкой. Пункт
// «до утра» - до 09:00 следующего утра по часам браузера.
const MUTE_CHOICES = [
  ["", "🔔 Тревоги включены"],
  ["60", "🔕 Не тревожить 1 ч"],
  ["240", "🔕 Не тревожить 4 ч"],
  ["morning", "🔕 До утра (09:00)"],
  ["1440", "🔕 Не тревожить сутки"],
];

function minutesUntilMorning() {
  const now = new Date();
  const end = new Date(now);
  end.setHours(9, 0, 0, 0);
  if (end <= now) end.setDate(end.getDate() + 1);
  return Math.ceil((end - now) / 60000);
}

function muteControl(kind, target, mutedUntil) {
  const select = document.createElement("select");
  select.className = "mute-select";
  MUTE_CHOICES.forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  });
  if (mutedUntil) {
    const until = new Date(mutedUntil);
    const option = document.createElement("option");
    option.value = "keep";
    option.textContent = `🔕 Тихо до ${until.toLocaleString("ru-RU",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`;
    select.prepend(option);
    select.value = "keep";
    select.classList.add("muted");
  }
  select.title = "Окно обслуживания: тревоги в Telegram по этому объекту не приходят";
  select.onclick = (event) => event.stopPropagation();
  select.onchange = async () => {
    const value = select.value;
    if (value === "keep") return;
    const minutes = value === "morning" ? minutesUntilMorning() : Number(value || 0);
    try {
      await api("/api/v1/mutes", { method: "POST", body: JSON.stringify({ kind, target, minutes }) });
      await refresh();
    } catch (error) {
      showAlert(error.message);
    }
  };
  return select;
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

  // Адрес для реального входа - отдельной строкой, когда он отличается от
  // адреса проверки (обычно так у устройств за туннелем: проверяем по
  // настоящему IP в сети объекта, а входим через локальный проброшенный
  // порт, до которого доходит обычный браузер).
  // Куда реально ведёт «Открыть»: адрес, вписанный руками, или порт
  // встроенного релея для устройства за туннелем точки.
  const accessUrl = device.open_url || device.relay_url;
  let openHost = null;
  if (accessUrl && accessUrl !== device.url) {
    openHost = document.createElement("div");
    openHost.className = "card-host card-host-open";
    openHost.textContent = `вход: ${accessUrl}`;
    openHost.title = "Локальный адрес для входа — настоящий IP снаружи туннеля недоступен";
  }

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
  // Порядок важен. Если у устройства задан отдельный «адрес для открытия»
  // (обычно локальный порт, проброшенный туннелем), он выигрывает у всего
  // остального: это обычная вкладка, работает в любом браузере и не зависит
  // ни от установленного лаунчера, ни от схемы devmon://, ни от песочницы
  // snap, которая не пускает браузер к обработчикам хоста.
  const siteProxy = (siteInfo(device.site) || {}).effective_proxy;
  // Браузер умеет войти за нас сам, но только пока устройство спрашивает
  // пароль по HTTP - ответом 401 с заголовком WWW-Authenticate. Проверено на
  // Chrome 152: адрес вида http://логин:пароль@хост/ проходит и с Basic, и с
  // Digest при переходе верхнего уровня. Если же вход нарисован формой внутри
  // страницы (так делает веб-морда Dahua), подставить учётку снаружи нельзя
  // ничем: страница живёт в чужом origin, и лезть в её поля браузер не даст
  // никому. Там остаётся буфер обмена - одна вставка вместо набора пароля.
  const autoLogin = device.has_password && AUTOLOGIN_SCHEMES.includes(webAuthOf(device));

  if (accessUrl) {
    open.onclick = () => openDevice(device, accessUrl, autoLogin, open);
    open.title = device.open_url
      ? `Откроется по заданному адресу: ${device.open_url}`
      : `Откроется через туннель точки: ${accessUrl}`;
  } else if (siteProxy) {
    // Учётку через лаунчер не передаём намеренно: она ушла бы в аргументы
    // процесса и стала бы видна в `ps` любому пользователю машины. Здесь
    // работает только буфер обмена.
    open.onclick = () => {
      const link = "devmon://open?site=" + encodeURIComponent(device.site)
        + "&proxy=" + encodeURIComponent(siteProxy)
        + "&url=" + encodeURIComponent(device.url);
      window.location.href = link;
      if (device.has_password) copyDevicePassword(device, open);
    };
    open.title = "Откроется в профиле точки через её туннель (нужен установленный обработчик)";
  } else {
    open.onclick = () => openDevice(device, device.url, autoLogin, open);
  }
  if (device.has_password) {
    open.title = autoLogin
      ? `Вход подставится сам (${webAuthOf(device)}, логин ${device.username || "не задан"})`
      : (open.title ? open.title + ". " : "") + "Пароль ляжет в буфер — вставьте в форму входа";
  }

  // Видео показываем только когда есть обе половины адреса: где стоит
  // go2rtc (у точки) и как называется источник (у устройства). Одной из них
  // мало — кнопка вела бы в пустоту.
  const streamBase = (siteInfo(device.site) || {}).stream_base;
  if (streamBase && device.stream_name) {
    const watch = document.createElement("button");
    watch.type = "button";
    watch.textContent = "Смотреть";
    watch.title = "Открыть поток в новой вкладке";
    watch.onclick = () => window.open(
      `/static/streams.html?site=${encodeURIComponent(device.site)}&device=${encodeURIComponent(device.id)}`,
      "_blank", "noopener");
    actions.append(watch);
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
      await api(`/api/v1/devices/${device.id}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      showAlert(error.message);
    }
  };

  actions.append(open, info, edit, copy, remove, muteControl("device", device.id, device.muted_until));
  card.append(head, host);
  if (openHost) card.append(openHost);
  card.append(ports, meta);

  // Раскрывающийся блок «Доп. инфо»: сведения, которые устройство рассказало о
  // себе (модель, серийник, прошивка, MAC, отпечаток). Заполняется из facts —
  // вендор и модель Hikvision видны без пароля, остальное приходит по учётке.
  const facts = device.facts || {};
  // Проблемы здоровья (часы, NTP) - строкой на самой карточке, а не только в
  // свёрнутом «Доп. инфо»: иначе их никто не увидит.
  if (facts.health) {
    const warn = document.createElement("div");
    warn.className = "card-health";
    warn.textContent = `⚠ ${facts.health}`;
    meta.after(warn);
  }
  const rows = FACT_ORDER.filter((k) => facts[k] && k !== "detail" && k !== "checked_at");
  const extra = document.createElement("details");
  extra.className = "more";
  const sum = document.createElement("summary");
  sum.textContent = rows.length ? "Доп. инфо" : "Доп. инфо — нет данных";
  extra.append(sum);
  if (rows.length) {
    const dl = document.createElement("dl");
    dl.className = "more-list";
    rows.forEach((k) => {
      const dt = document.createElement("dt");
      dt.textContent = FACT_LABELS[k];
      const dd = document.createElement("dd");
      dd.textContent = factText(k, facts[k]);
      dl.append(dt, dd);
    });
    extra.append(dl);
  } else {
    const hint = document.createElement("p");
    hint.className = "more-hint";
    hint.textContent = facts.detail
      ? facts.detail
      : "Сведения появятся после опроса устройства (модель, серийник, прошивка). "
        + "Серийник и прошивку устройство отдаёт по логину — задайте его в «Настроить».";
    extra.append(hint);
  }
  card.append(extra);

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
  const connect = document.createElement("button");
  connect.type = "button";
  connect.className = "primary";
  connect.textContent = "+ Подключить объект";
  connect.onclick = openConnect;
  els.view.append(screenHead("Точки", "Выберите точку, чтобы увидеть её оборудование.", "", connect));

  if (topologyView) {
    els.view.append(renderTopology());
    return;
  }

  const grid = document.createElement("div");
  grid.className = "site-grid";
  siteNames().forEach((site) => {
    const meta = siteInfo(site);
    grid.append(
      groupTile({
        title: site,
        items: devicesOf(site, null),
        note: meta.note,
        tunnel: meta.effective_proxy ? (meta.tunnel || "unknown") : null,
        openLabel: "Открыть точку →",
        onOpen: () => goTo(site, null),
      })
    );
  });
  grid.append(newTile("Подключить объект", openConnect));
  els.view.append(grid);
}

// ---------------------------------------------------------------------------
// вид «Топология»: те же данные, что список точек, графом хаб-спицы вместо
// карточек. Переключатель хранится в localStorage — выбор на этой машине,
// не настройка сервиса.
// ---------------------------------------------------------------------------

// Переключатель живёт в верхней панели, а не на экране «Точки»: адрес
// страницы помнит последнюю открытую точку, и на экране объекта кнопка
// с экрана списка была бы просто не видна.
function setupViewToggle() {
  const box = document.getElementById("view-toggle");
  if (!box) return;
  box.querySelectorAll("button").forEach((button) => {
    button.onclick = () => setTopologyView(button.dataset.view === "topology");
  });
}

function syncViewToggle() {
  const box = document.getElementById("view-toggle");
  if (!box) return;
  box.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", (button.dataset.view === "topology") === topologyView);
  });
}

function setTopologyView(value) {
  topologyView = value;
  try { localStorage.setItem(VIEW_KEY, value ? "topology" : "list"); } catch (e) { /* приватный режим */ }
  // Оба вида — про верхний уровень. Если сейчас открыта точка, переключение
  // вида без возврата наверх выглядело бы как «кнопка ничего не делает».
  if (route().site) goTo(null);
  else render();
}

function renderTopology() {
  const wrap = document.createElement("div");
  wrap.className = "topo-grid";
  const names = siteNames();
  if (!names.length) {
    const empty = document.createElement("p");
    empty.className = "site-note";
    empty.textContent = "Пока нет точек.";
    wrap.append(empty);
    return wrap;
  }
  names.forEach((site) => {
    const meta = siteInfo(site);
    const items = devicesOf(site, null);
    const tunnelDown = meta.effective_proxy ? meta.tunnel === "down" : false;
    // Хаб красит siteOffline, а не только состояние туннеля: у точки без
    // прокси туннеля нет вовсе, но если не отвечает ни одно устройство —
    // связи с объектом нет, и хаб не должен выглядеть живым. Та же функция
    // решает, показывать ли баннер сверху, — один смысл, одна логика.
    wrap.append(topoCard(site, meta, items, tunnelDown, siteOffline(site)));
  });
  return wrap;
}

function topoCard(site, meta, items, tunnelDown, offline) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "topo-card";
  card.title = `Открыть точку «${site}»`;
  card.onclick = () => goTo(site, null);

  const head = document.createElement("div");
  head.className = "topo-card-head";
  const name = document.createElement("span");
  name.className = "topo-card-name";
  name.textContent = site;
  head.append(name);
  if (meta.effective_proxy) {
    const chip = document.createElement("span");
    chip.className = `topo-chip ${tunnelDown ? "down" : "up"}`;
    chip.textContent = tunnelDown ? "туннель: down" : "туннель: up";
    head.append(chip);
  }
  card.append(head);
  card.append(topoDiagram(items, tunnelDown, offline));

  const down = items.filter((d) => d.status === "down").length;
  const count = document.createElement("div");
  count.className = "topo-card-count" + (offline ? " offline" : "");
  if (tunnelDown) count.textContent = "нет связи с туннелем";
  else if (offline) count.textContent = `нет связи с объектом · ${countLabel(items)}`;
  else if (down) count.textContent = `${countLabel(items)} · не отвечают ${down}`;
  else count.textContent = countLabel(items);
  card.append(count);

  return card;
}

function shortLabel(name) {
  const parts = name.split(",");
  const label = parts.length > 1 ? parts[parts.length - 1].trim() : name.split(" ")[0];
  return label.length > 11 ? label.slice(0, 10) + "…" : label;
}

function topoDiagram(items, tunnelDown, offline) {
  const NS = "http://www.w3.org/2000/svg";
  const W = 280, hubX = W / 2, hubY = 38, rowY = 126;
  const n = items.length;
  const marginX = 26;
  const step = n > 1 ? (W - marginX * 2) / (n - 1) : 0;

  // Подписи влезают только пока узлов мало. На 14 устройствах они налезают
  // друг на друга и превращаются в кашу, поэтому дальше остаются одни точки,
  // а имя и статус читаются наведением.
  const slot = n > 1 ? step : W;
  const withLabels = slot >= 40;
  const radius = slot >= 26 ? 8 : 5;
  const H = withLabels ? 168 : 148;

  function el(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "topo-diagram", role: "img" });
  const hubState = offline ? "down" : "up";

  svg.append(el("line", {
    x1: hubX, y1: 2, x2: hubX, y2: hubY - 11,
    class: `topo-trunk ${hubState}`,
  }));

  if (!offline) svg.append(el("circle", { cx: hubX, cy: hubY, r: 12, class: "topo-hub-ring" }));
  svg.append(el("circle", { cx: hubX, cy: hubY, r: 9, class: `topo-hub ${hubState}` }));

  if (!items.length) {
    const t = el("text", { x: hubX, y: rowY, class: "topo-empty", "text-anchor": "middle" });
    t.textContent = "нет устройств";
    svg.append(t);
    return svg;
  }

  items.forEach((device, i) => {
    const x = n > 1 ? marginX + step * i : hubX;
    const status = tunnelDown ? "down" : device.status;
    svg.append(el("line", { x1: hubX, y1: hubY + 7, x2: x, y2: rowY - radius, class: "topo-spoke" }));

    const node = el("circle", { cx: x, cy: rowY, r: radius, class: `topo-node ${status}` });
    const title = el("title", {});
    const latency = status === "up" && device.latency_ms != null ? `, ${device.latency_ms} мс` : "";
    title.textContent = `${device.name} — ${STATE_LABEL[status] || STATE_LABEL.unknown}${latency}`;
    node.append(title);
    svg.append(node);

    if (!withLabels) return;

    const label = el("text", { x, y: rowY + 16, class: "topo-node-label", "text-anchor": "middle" });
    label.textContent = shortLabel(device.name);
    svg.append(label);

    if (status === "up" && device.latency_ms != null) {
      const sub = el("text", { x, y: rowY + 25, class: "topo-node-sub", "text-anchor": "middle" });
      sub.textContent = `${device.latency_ms}мс`;
      svg.append(sub);
    }
  });

  return svg;
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
  // Мозаика всех камер точки — отдельной вкладкой. Показываем кнопку только
  // если у точки задан go2rtc и есть хоть одна камера с именем потока.
  const withStream = all.filter((d) => d.stream_name).length;
  const mute = muteControl("site", site, meta.muted_until);
  const find = document.createElement("button");
  find.type = "button";
  find.textContent = "Найти оборудование";
  find.title = "Скан подсети точки: что отвечает и что это за устройство";
  find.onclick = () => openScan(site);
  let actions = document.createElement("div");
  actions.className = "head-actions";
  actions.append(mute, find, settings);
  if (meta.stream_base && withStream) {
    const watchAll = document.createElement("button");
    watchAll.type = "button";
    watchAll.className = "primary";
    watchAll.textContent = `Смотреть камеры (${withStream})`;
    watchAll.onclick = () => window.open(
      `/static/streams.html?site=${encodeURIComponent(site)}`, "_blank", "noopener");
    // Обе кнопки одним узлом: screenHead принимает ровно один элемент-действие,
    // а вставить рядом нельзя — он ещё не в документе.
    actions.prepend(watchAll);
  }
  els.view.append(
    crumb("← Все точки", () => goTo(null)),
    screenHead(site, `${countLabel(all)} · ${badgeLabel(all)}`, meta.note, actions)
  );
  if (meta.effective_proxy && meta.tunnel === "down") {
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

function renderSummary(scope, local) {
  const up = scope.filter((d) => d.status === "up").length;
  const down = scope.filter((d) => d.status === "down").length;
  els.summary.innerHTML = scope.length
    ? `<span>${local ? "здесь" : "устройств"} <b>${scope.length}</b></span>` +
      `<span class="s-up">на связи <b>${up}</b></span>` +
      `<span class="s-down">не отвечают <b>${down}</b></span>`
    : "";
  const downEl = els.summary.querySelector(".s-down");
  if (downEl && down) {
    downEl.classList.add("link");
    downEl.title = "Показать всех, кто не отвечает";
    downEl.onclick = () => { location.hash = "down=1"; };
  }
  els.updated.textContent = state.updated_at ? `проверено ${ago(state.updated_at)} назад` : "";
}

// ---------------------------------------------------------------------------
// поиск и «все, кто не отвечает» - поперёк точек
// ---------------------------------------------------------------------------

function matchesQuery(device, words) {
  const facts = device.facts || {};
  const haystack = [device.name, device.host, device.site, device.category, device.note,
    device.open_url, facts.model, facts.serial, facts.vendor]
    .filter(Boolean).join(" ").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

function renderFindScreen(query, onlyDown) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const found = (state.devices || [])
    .filter((d) => !onlyDown || d.status === "down")
    .filter((d) => !words.length || matchesQuery(d, words));

  const title = onlyDown && !query ? "Не отвечают" : `Поиск «${query}»`;
  const subtitle = found.length
    ? `${countLabel(found)} на ${new Set(found.map((d) => d.site)).size} точках`
    : "ничего не нашлось";
  els.view.append(
    crumb("← Все точки", () => { location.hash = ""; }),
    screenHead(title, subtitle, "", null)
  );

  // По точкам: «не отвечает весь объект» и «одна камера» - разные проблемы,
  // и видно это, только когда устройства сгруппированы.
  const bySite = new Map();
  found.sort((a, b) => a.site.localeCompare(b.site, "ru") || a.name.localeCompare(b.name, "ru"))
    .forEach((d) => {
      if (!bySite.has(d.site)) bySite.set(d.site, []);
      bySite.get(d.site).push(d);
    });
  bySite.forEach((items, siteName) => {
    const info = siteInfo(siteName) || {};
    const total = devicesOf(siteName, null).length;
    const head = document.createElement("h2");
    head.className = "find-site";
    const link = document.createElement("a");
    link.href = "#" + new URLSearchParams({ site: siteName }).toString();
    link.textContent = siteName;
    const meta = document.createElement("span");
    meta.className = "find-site-meta";
    meta.textContent = onlyDown
      ? ` — не отвечают ${items.length} из ${total}${info.tunnel === "down" ? " · нет связи с туннелем" : ""}`
      : ` — ${countLabel(items)}`;
    head.append(link, meta);
    if (info.muted_until) {
      const muted = document.createElement("span");
      muted.className = "find-site-meta";
      muted.textContent = " · 🔕 тревоги выключены";
      head.append(muted);
    }
    const grid = document.createElement("div");
    grid.className = "grid";
    items.forEach((device) => grid.append(deviceCard(device)));
    els.view.append(head, grid);
  });
}

const searchBox = document.getElementById("search");
let searchTimer = null;

function syncSearchBox(query) {
  // Пока человек печатает, поле не трогаем - иначе перерисовка раз в
  // несколько секунд сбивала бы курсор.
  if (document.activeElement !== searchBox) searchBox.value = query;
}

searchBox.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    // replaceState, а не location.hash: иначе каждая пауза при наборе
    // ложилась бы в историю, и «Назад» шагал бы по буквам.
    const text = searchBox.value.trim();
    const hash = text ? "#" + new URLSearchParams({ q: text }).toString() : "";
    history.replaceState(null, "", hash || location.pathname);
    render();
  }, 250);
});
searchBox.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    searchBox.value = "";
    history.replaceState(null, "", location.pathname);
    render();
    searchBox.blur();
  }
});
document.addEventListener("keydown", (event) => {
  // «/» - быстрый переход в поиск, как в GitHub и Gmail.
  if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey
      && !event.target.closest("input, textarea, select, dialog")) {
    event.preventDefault();
    searchBox.focus();
  }
});

// ---------------------------------------------------------------------------
// тема и меню «⋯»
// ---------------------------------------------------------------------------

const THEME_KEY = "dm_theme";
const THEMES = [["", "Тема: как в системе"], ["light", "Тема: светлая"], ["dark", "Тема: тёмная"]];

function applyTheme(value) {
  if (value) document.documentElement.dataset.theme = value;
  else delete document.documentElement.dataset.theme;
  const label = (THEMES.find(([v]) => v === value) || THEMES[0])[1];
  document.getElementById("theme").textContent = label;
}

// Текущая тема держится в переменной: без localStorage (приватный режим)
// переключатель иначе залипал бы, всегда читая «как в системе».
let currentTheme = (() => {
  try { return localStorage.getItem(THEME_KEY) || ""; } catch (e) { return ""; }
})();

applyTheme(currentTheme);
document.getElementById("theme").onclick = () => {
  const order = THEMES.map(([v]) => v);
  currentTheme = order[(order.indexOf(currentTheme) + 1) % order.length];
  try { localStorage.setItem(THEME_KEY, currentTheme); } catch (e) { /* приватный режим */ }
  applyTheme(currentTheme);
};

const moreMenu = document.getElementById("more-menu");
moreMenu.querySelectorAll(".menu button").forEach((button) => {
  // Тему переключают несколько раз подряд - меню для неё не закрываем.
  if (button.id !== "theme") button.addEventListener("click", () => { moreMenu.open = false; });
});
document.addEventListener("click", (event) => {
  if (moreMenu.open && !moreMenu.contains(event.target)) moreMenu.open = false;
});

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

// Точка считается «без связи», когда у неё есть устройства и НИ ОДНО не на
// связи (или лёг её туннель). Это характерный признак обрыва VPN/туннеля до
// объекта, а не поломки одного устройства — и повод показать подсказку.
function siteOffline(name) {
  const items = devicesOf(name, null);
  if (!items.length) return false;
  const info = siteInfo(name);
  if (info.effective_proxy && info.tunnel === "down") return true;
  return items.every((d) => d.status === "down");
}

function offlineBanner() {
  const { site } = route();
  const dead = site
    ? (siteOffline(site) ? [site] : [])
    : siteNames().filter(siteOffline);
  if (!dead.length) return;
  const many = dead.length > 1;
  // Имя точки вводит пользователь, то есть это чужой текст. Через innerHTML
  // точка с именем вида <img src=x onerror=…> исполняла бы свой код в origin
  // панели — а панель умеет отдавать /secret с паролями от железа. Поэтому
  // здесь собираются узлы, а не строка разметки.
  els.alert.textContent = "";
  const strong = document.createElement("b");
  strong.textContent = `Нет связи ${many ? "с объектами" : "с объектом"}: ${dead.join(", ")}.`;
  els.alert.append(strong, document.createTextNode(
    " Проверьте, включён ли VPN до объекта (или поднят ли SSH-туннель). "
    + "Пока связи нет, устройства показываются недоступными, даже если они работают."));
  els.alert.hidden = false;
}

function render() {
  els.view.textContent = "";
  const devices = state.devices || [];
  const hasAnything = devices.length > 0 || (state.sites || []).length > 0;
  els.empty.hidden = hasAnything;

  const { site, category, query, onlyDown } = route();
  syncViewToggle();
  syncSearchBox(query);
  const downTotal = devices.filter((d) => d.status === "down").length;
  // Число упавших - в заголовке вкладки: видно, даже когда панель в фоне.
  document.title = downTotal ? `(${downTotal}) Device Monitor` : "Device Monitor";

  const finding = hasAnything && !!(query || onlyDown);

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

  renderSummary(site && !finding ? devicesOf(site, category) : devices, !!site && !finding);

  // Без раннего return: баннер про VPN и подсказки точек/категорий ниже
  // нужны и на экране поиска - особенно на «не отвечают».
  if (finding) renderFindScreen(query, onlyDown);
  else if (hasAnything) {
    if (site && category !== null) renderDevicesScreen(site, category);
    else if (site) renderCategoriesScreen(site);
    else renderSitesScreen();
  }

  // Баннер про VPN — после отрисовки, поверх пустого alert. Если сервис
  // отвечает, но объект недоступен, обычную ошибку это не затирает: она
  // выставляется только в catch refresh().
  offlineBanner();

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
    state = await api("/api/v1/state");
    markAlertsButton();
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
  // У точки с доступом «через SSH» прокси держит сервис - поле руками не правится.
  document.getElementById("g-proxy-field").hidden = !isSite || info.access === "ssh";
  groupForm.proxy.value = isSite ? (info.proxy || "") : "";
  document.getElementById("g-stream-field").hidden = !isSite;
  groupForm.stream_base.value = isSite ? (info.stream_base || "") : "";
  // Учётка - тоже только у точки. Пароль write-only, ровно как у устройства:
  // в форме всегда пусто, а плейсхолдер говорит, задан ли он.
  document.getElementById("g-creds-field").hidden = !isSite;
  groupForm.username.value = isSite ? (info.username || "") : "";
  groupForm.password.value = "";
  groupForm.password.disabled = false;
  groupForm.password.type = "password";
  if (groupEye) groupEye.innerHTML = EYE;
  groupHasPassword = !!(isSite && name && info.has_password);
  groupClearPassword = false;
  groupForm.password.placeholder = groupHasPassword ? "нажмите глаз, чтобы показать сохранённый" : "";
  document.getElementById("g-password-hint").textContent =
    "Общая учётка объекта. Устройства берут её, только если у них это включено явно.";
  syncClearButton("g", groupHasPassword, false);
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
  if (isSite) {
    payload.proxy = groupForm.proxy.value;
    payload.stream_base = groupForm.stream_base.value;
    payload.username = groupForm.username.value;
    // Пустое поле = «не менять», false = «удалить», см. clean_password().
    if (groupForm.password.value) payload.password = groupForm.password.value;
    else if (groupClearPassword) payload.password = false;
  }
  try {
    let result;
    if (isSite) {
      result = groupOriginal
        ? await api(`/api/v1/sites/${encodeURIComponent(groupOriginal)}`, {
            method: "PUT", body: JSON.stringify(payload),
          })
        : await api("/api/v1/sites", { method: "POST", body: JSON.stringify(payload) });
    } else {
      result = groupOriginal
        ? await api(
            `/api/v1/categories/${encodeURIComponent(groupSite)}/${encodeURIComponent(groupOriginal)}`,
            { method: "PUT", body: JSON.stringify(payload) }
          )
        : await api("/api/v1/categories", {
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
        ? `/api/v1/sites/${encodeURIComponent(groupOriginal)}`
        : `/api/v1/categories/${encodeURIComponent(groupSite)}/${encodeURIComponent(groupOriginal)}`,
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

let editingHasPassword = false;
let editingClearPassword = false;
let groupHasPassword = false;
let groupClearPassword = false;

/** Кнопка «Удалить сохранённый пароль»: видна, пока пароль есть. Нажатие
 *  только помечает удаление - сохраняется оно вместе с формой. */
function syncClearButton(prefix, hasPassword, clearing) {
  const button = document.getElementById(`${prefix}-password-clear`);
  const hint = document.getElementById(`${prefix}-password-hint`);
  button.hidden = !hasPassword;
  button.textContent = clearing ? "Не удалять пароль" : "Удалить сохранённый пароль";
  if (clearing) hint.textContent = "Пароль будет удалён при сохранении.";
}

document.getElementById("f-password-clear").onclick = () => {
  editingClearPassword = !editingClearPassword;
  els.form.password.value = "";
  els.form.password.disabled = editingClearPassword;
  if (!editingClearPassword) {
    document.getElementById("f-password-hint").textContent =
      "Пароль сохранён. Глаз — показать текущий; впишите другой — заменить; оставите пустым — не изменится.";
  }
  syncClearButton("f", true, editingClearPassword);
};

document.getElementById("g-password-clear").onclick = () => {
  groupClearPassword = !groupClearPassword;
  groupForm.password.value = "";
  groupForm.password.disabled = groupClearPassword;
  if (!groupClearPassword) {
    document.getElementById("g-password-hint").textContent =
      "Общая учётка объекта. Устройства берут её, только если у них это включено явно.";
  }
  syncClearButton("g", true, groupClearPassword);
};

function openEditor(device, preset, asCopy) {
  editingId = asCopy ? null : device ? device.id : null;
  // Форма работает с СОБСТВЕННОЙ учёткой устройства. Эффективная (has_password)
  // может прийти от точки: заполни форму ею - и при сохранении пароль точки
  // записался бы как собственный пароль устройства.
  editingHasPassword = !!(device && device.own_has_password) && !asCopy;
  editingClearPassword = false;
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
  els.form.username.value = device ? device.own_username || "" : "";
  // Пароль write-only: в форме всегда пусто, существующий не показываем и не
  // отдаём наружу. Плейсхолдер сообщает, задан ли он и что пустое поле = «не
  // менять». У копии пароль не переносим (это другое устройство).
  els.form.password.value = "";
  els.form.password.disabled = false;
  els.form.password.type = "password";
  if (pwEye) pwEye.innerHTML = EYE;
  const hasPass = editingHasPassword;
  syncClearButton("f", hasPass, false);
  els.form.password.placeholder = hasPass ? "нажмите глаз, чтобы показать сохранённый" : "";
  document.getElementById("f-password-hint").textContent = hasPass
    ? "Пароль сохранён. Глаз — показать текущий; впишите другой — заменить; оставите пустым — не изменится."
    : "Нужен для сбора сведений с устройства (модель, серийник, прошивку).";
  inheritBox.checked = !!(device && device.use_site_creds);
  syncInherit();
  els.form.open_url.value = device ? device.open_url || "" : "";
  els.form.stream_name.value = device ? device.stream_name || "" : "";
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
    open_url: els.form.open_url.value,
    stream_name: els.form.stream_name.value,
    username: els.form.username.value,
    use_site_creds: inheritBox.checked,
    note: els.form.note.value,
  };
  // Пароль шлём только если поле заполнено: пустое = «не менять», false =
  // «удалить» (см. clean_password() на бэкенде).
  if (els.form.password.value) payload.password = els.form.password.value;
  else if (editingClearPassword) payload.password = false;
  try {
    const result = editingId
      ? await api(`/api/v1/devices/${editingId}`, { method: "PUT", body: JSON.stringify(payload) })
      : await api("/api/v1/devices", { method: "POST", body: JSON.stringify(payload) });
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

// Глаз у поля пароля: переключает видимость. Иконки рисуем прямо здесь, чтобы
// не тащить набор иконок ради одной кнопки.
const EYE = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M9.9 4.2A11 11 0 0 1 12 4c7 0 11 7 11 7a18 18 0 0 1-3 3.5M6.6 6.6A18 18 0 0 0 1 12s4 7 11 7a11 11 0 0 0 4-.7"/><path d="m3 3 18 18"/></svg>';
const pwEye = document.getElementById("f-password-eye");
const groupEye = document.getElementById("g-password-eye");
const inheritBox = document.getElementById("f-use-site-creds");

/** Когда устройство берёт учётку точки, свои поля логина и пароля не нужны. */
function syncInherit() {
  const on = inheritBox.checked;
  document.getElementById("f-creds-row").hidden = on;
  document.getElementById("f-inherit-hint").textContent = on
    ? "Логин и пароль берутся из настроек точки — парой, а не по отдельности."
    : "У камер объекта пароль обычно общий — заведите его один раз в настройках точки.";
}

inheritBox.addEventListener("change", () => {
  if (inheritBox.checked) {
    // Свои поля очищаем, а сохранённый пароль помечаем к удалению: пустое
    // поле на бэкенде значит «не менять», и без этого пароль, которым мы
    // больше не пользуемся, остался бы лежать в базе.
    els.form.username.value = "";
    els.form.password.value = "";
    editingClearPassword = editingHasPassword;
  } else {
    editingClearPassword = false;
  }
  syncInherit();
});

if (groupEye) {
  groupEye.innerHTML = EYE;
  groupEye.onclick = async () => {
    const field = groupForm.password;
    // Как у устройства: пустое поле и сохранённый пароль - глаз подгружает
    // его. Раньше здесь только переключался тип пустого поля, и пароль точки
    // увидеть было нельзя вовсе.
    if (!field.value && groupHasPassword && groupOriginal && !groupClearPassword) {
      groupEye.disabled = true;
      try {
        const data = await api(`/api/v1/sites/${encodeURIComponent(groupOriginal)}/secret`);
        field.value = data.password || "";
      } catch (error) {
        groupError.textContent = error.message;
        groupError.hidden = false;
        return;
      } finally {
        groupEye.disabled = false;
      }
    }
    const shown = field.type === "text";
    field.type = shown ? "password" : "text";
    groupEye.innerHTML = shown ? EYE : EYE_OFF;
  };
}
pwEye.innerHTML = EYE;
pwEye.onclick = async () => {
  const field = els.form.password;
  // Поле пустое, но у устройства пароль сохранён — глаз его подгружает и
  // показывает (через отдельный эндпоинт /secret, залогиненному). Дальше
  // просто переключает видимость.
  if (!field.value && editingHasPassword && editingId && !editingClearPassword) {
    pwEye.disabled = true;
    try {
      const data = await api(`/api/v1/devices/${editingId}/secret?own=true`);
      field.value = data.password || "";
    } catch (error) {
      // Ошибку - в саму форму: баннер страницы лежит под модальным окном,
      // и раньше глаз из-за этого «молча ничего не делал».
      els.formError.textContent = error.message;
      els.formError.hidden = false;
      return;
    } finally {
      pwEye.disabled = false;
    }
  }
  const shown = field.type === "text";
  field.type = shown ? "password" : "text";
  pwEye.innerHTML = shown ? EYE : EYE_OFF;
  pwEye.title = shown ? "Показать пароль" : "Скрыть пароль";
  pwEye.setAttribute("aria-label", pwEye.title);
};

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
  health: "Требует внимания",
  clock: "Часы",
  ntp: "Синхронизация времени",
  fingerprint: "Отпечаток (не меняется при смене IP)",
  web_auth: "Как спрашивает пароль",
  detail: "Примечание",
};

// Значения, которые в сыром виде ничего не говорят человеку. Показываем не
// «basic», а что из этого следует для кнопки «Открыть».
const FACT_VALUES = {
  web_auth: {
    basic: "HTTP Basic — вход подставится сам",
    digest: "HTTP Digest — вход подставится сам",
    form: "форма на странице — пароль ляжет в буфер",
  },
};

function factText(key, value) {
  return (FACT_VALUES[key] && FACT_VALUES[key][value]) || value;
}
const FACT_ORDER = ["health", "vendor", "model", "serial", "firmware", "released", "hardware",
                    "mac", "clock", "ntp", "device_name", "fingerprint", "web_auth", "detail"];

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
      // execCommand не бросает исключение, а возвращает false. Без этой
      // проверки кнопка рапортовала «Скопировано» там, где буфер остался
      // пустым, и человек вставлял в форму устройства прошлое содержимое.
      const done = document.execCommand("copy");
      ta.remove();
      if (!done) throw new Error("буфер обмена недоступен");
    }
    button.textContent = "Скопировано";
  } catch (e) {
    button.textContent = "Не вышло";
  }
  setTimeout(() => { button.textContent = original; }, 1500);
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
    if (facts[key]) dl.append(...row(FACT_LABELS[key], factText(key, facts[key])));
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

  // История доступности
  try {
    const data = await api(`/api/v1/devices/${device.id}/history`);
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
    await api(`/api/v1/devices/${detailId}/inspect`, { method: "POST" });
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

// --- аккаунт: выход и смена учётки ---------------------------------------
document.getElementById("logout").onclick = async () => {
  try { await api("/api/v1/logout", { method: "POST" }); } catch (e) {}
  location.href = "/";
};

// Экспорт и импорт всех данных. Экспорт - обычная навигация: браузер сам
// сохранит файл (Content-Disposition), страница при этом остаётся на месте.
document.getElementById("all-streams").onclick = () => {
  window.open("/static/streams.html", "_blank", "noopener");
};

document.getElementById("export").onclick = () => {
  window.location.href = "/api/v1/export";
};

document.getElementById("export-secrets").onclick = () => {
  if (!confirm("В файле будут пароли устройств в открытом виде. Храните его как секрет. Выгрузить?")) return;
  window.location.href = "/api/v1/export?secrets=true";
};

const importFile = document.getElementById("import-file");
document.getElementById("import").onclick = () => importFile.click();

importFile.onchange = async () => {
  const file = importFile.files[0];
  importFile.value = "";
  if (!file) return;
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (error) {
    showAlert("Файл не похож на JSON из экспорта Device Monitor");
    return;
  }
  try {
    const report = await api("/api/v1/import", { method: "POST", body: JSON.stringify(payload) });
    const errors = report.errors.length
      ? `. Не перенесено (${report.errors.length}): ${report.errors.slice(0, 5).join("; ")}`
      : "";
    showAlert(`Импорт: точек ${report.sites}, категорий ${report.categories}, устройств ${report.devices}${errors}`);
    await refresh();
  } catch (error) {
    showAlert(error.message);
  }
};

// ---------------------------------------------------------------------------
// оповещения
// ---------------------------------------------------------------------------

const alertsEditor = document.getElementById("alerts-editor");
const alertsForm = document.getElementById("alerts-form");
const alertsError = document.getElementById("alerts-error");
const alertsStatus = document.getElementById("al-status");

function alertsFail(error) {
  alertsError.textContent = error.message;
  alertsError.hidden = false;
}

function fillAlerts(s) {
  alertsForm.telegram_token.value = "";
  alertsForm.telegram_token.placeholder = s.has_token
    ? (s.token_from_env ? "задан в окружении (DM_TG_TOKEN)" : "сохранён — пусто = не менять")
    : "";
  document.getElementById("al-token-hint").textContent = s.bot
    ? `Бот: ${s.bot}` : "Выдаёт @BotFather, вида 123456789:AA…";
  alertsForm.chat_id.value = s.chat_id || "";
  alertsForm.chat_id.dataset.title = s.chat_title || "";
  document.getElementById("al-chat-hint").textContent = s.chat_title
    ? `Группа: ${s.chat_title}` : "Ссылка-приглашение не подойдёт — нужен числовой ID, его найдёт кнопка.";
  alertsForm.down_confirm.value = s.down_confirm;
  alertsForm.repeat_hours.value = s.repeat_hours;
  alertsForm.heartbeat_url.value = s.heartbeat_url || "";
  alertsForm.enabled.checked = s.enabled;
  const wait = s.down_confirm * s.poll_interval;
  document.getElementById("al-rules-hint").textContent =
    `Тревога — после ${s.down_confirm} проверок подряд без ответа (≈${Math.round(wait / 60 * 10) / 10} мин). `
    + "Если лёг туннель объекта или не отвечает большая часть его устройств — одно сообщение про объект. "
    + "Повтор 0 — не повторять.";
  const when = (iso) => new Date(iso).toLocaleString("ru-RU");
  const parts = [];
  if (s.last_error) parts.push(`Telegram: ошибка — ${s.last_error}`);
  else if (s.last_ok) parts.push(`Telegram: последняя отправка ${when(s.last_ok)}`);
  if (s.heartbeat_url) {
    if (s.heartbeat_error) parts.push(`Сторож: ошибка — ${s.heartbeat_error}`);
    else if (s.heartbeat_ok) parts.push(`Сторож: последний пинг ${when(s.heartbeat_ok)}`);
  }
  alertsStatus.textContent = parts.join(" · ");
}

async function loadAlerts() {
  fillAlerts(await api("/api/v1/alerts"));
}

// Без настроенных оповещений кнопка подсвечивается: о падениях сейчас никто
// не узнает, пока не откроет панель.
function markAlertsButton() {
  const button = document.getElementById("alerts");
  const ok = state.alerts_configured;
  button.classList.toggle("warn", !ok);
  button.textContent = ok ? "Оповещения" : "Оповещения ⚠";
  button.title = ok ? "Оповещения о падениях в Telegram"
    : "Оповещения не настроены — о падениях никто не узнает";
}

document.getElementById("alerts").onclick = async () => {
  alertsError.hidden = true;
  document.getElementById("al-chats-field").hidden = true;
  try {
    await loadAlerts();
  } catch (error) {
    alertsFail(error);
  }
  alertsEditor.showModal();
};

document.getElementById("alerts-cancel").onclick = () => alertsEditor.close();

function alertsPayload() {
  const payload = {
    chat_id: alertsForm.chat_id.value.trim(),
    chat_title: alertsForm.chat_id.dataset.title || "",
    down_confirm: Number(alertsForm.down_confirm.value || 3),
    repeat_hours: Number(alertsForm.repeat_hours.value || 0),
    heartbeat_url: alertsForm.heartbeat_url.value.trim(),
    enabled: alertsForm.enabled.checked,
  };
  const token = alertsForm.telegram_token.value.trim();
  if (token) payload.telegram_token = token;
  return payload;
}

async function saveAlerts() {
  const saved = await api("/api/v1/alerts", { method: "PUT", body: JSON.stringify(alertsPayload()) });
  fillAlerts(saved);
  return saved;
}

alertsForm.chat_id.addEventListener("input", () => { alertsForm.chat_id.dataset.title = ""; });

document.getElementById("al-find").onclick = async (event) => {
  const button = event.currentTarget;
  alertsError.hidden = true;
  button.disabled = true;
  try {
    // Токен мог быть только что вписан - сначала сохраняем его.
    if (alertsForm.telegram_token.value.trim()) await saveAlerts();
    const { chats } = await api("/api/v1/alerts/chats", { method: "POST" });
    const select = document.getElementById("al-chats");
    select.textContent = "";
    if (!chats.length) {
      throw new Error("Бот пока не видел сообщений. Добавьте его в группу администратором, "
        + "напишите там /start и нажмите ещё раз.");
    }
    chats.forEach((chat) => {
      const option = document.createElement("option");
      option.value = chat.id;
      option.textContent = `${chat.title} (${chat.type}, ${chat.id})`;
      option.dataset.title = chat.title;
      select.append(option);
    });
    const pick = () => {
      const option = select.selectedOptions[0];
      alertsForm.chat_id.value = option.value;
      alertsForm.chat_id.dataset.title = option.dataset.title;
    };
    select.onchange = pick;
    const group = chats.find((c) => c.type === "group" || c.type === "supergroup");
    if (group) select.value = group.id;
    pick();
    document.getElementById("al-chats-field").hidden = false;
  } catch (error) {
    alertsFail(error);
  } finally {
    button.disabled = false;
  }
};

document.getElementById("al-test").onclick = async (event) => {
  const button = event.currentTarget;
  alertsError.hidden = true;
  button.disabled = true;
  try {
    await saveAlerts();
    await api("/api/v1/alerts/test", { method: "POST" });
    alertsStatus.textContent = "Тестовое сообщение отправлено — проверьте группу.";
  } catch (error) {
    alertsFail(error);
  } finally {
    button.disabled = false;
  }
};

alertsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  alertsError.hidden = true;
  try {
    await saveAlerts();
    alertsEditor.close();
    await refresh();
  } catch (error) {
    alertsFail(error);
  }
});

// ---------------------------------------------------------------------------
// мастер «Подключить объект»: доступ к сети объекта без терминала
// ---------------------------------------------------------------------------

const connectDialog = document.getElementById("connect-dialog");
const connectForm = document.getElementById("connect-form");
const connectError = document.getElementById("connect-error");
const connectStatus = document.getElementById("cn-status");

function connectFail(error) {
  connectError.textContent = error.message;
  connectError.hidden = false;
}

function connectKind() {
  return connectForm.querySelector("input[name=kind]:checked").value;
}

function syncConnectSteps() {
  const kind = connectKind();
  connectForm.querySelectorAll(".connect-step").forEach((step) => {
    step.hidden = step.dataset.kind !== kind;
  });
}

async function openConnect() {
  connectForm.reset();
  connectError.hidden = true;
  connectStatus.textContent = "";
  syncConnectSteps();
  try {
    const info = await api("/api/v1/access");
    const wg = connectForm.querySelector("input[value=wireguard]");
    const ssh = connectForm.querySelector("input[value=ssh]");
    wg.disabled = !info.networkmanager;
    ssh.disabled = !info.ssh;
    if (!info.networkmanager || !info.ssh) {
      connectStatus.textContent = "Часть вариантов недоступна: сервис работает в Docker, без доступа "
        + "к NetworkManager и ssh этой машины. В программе из .deb они есть.";
      connectForm.querySelector("input[value=direct]").checked = true;
      syncConnectSteps();
    }
  } catch (error) {
    connectFail(error);
  }
  connectDialog.showModal();
  connectForm.name.focus();
}

connectForm.querySelectorAll("input[name=kind]").forEach((radio) => {
  radio.addEventListener("change", syncConnectSteps);
});
document.getElementById("connect-cancel").onclick = () => connectDialog.close();

// SSH: проверить вход по ключу; не пускает - поставить ключ паролем (один раз).
async function ensureSshKey() {
  const target = document.getElementById("cn-ssh").value.trim();
  connectStatus.textContent = "Проверяю вход на сервер объекта…";
  let result = await api("/api/v1/access/ssh/test", { method: "POST", body: JSON.stringify({ target }) });
  if (!result.ok) {
    const password = document.getElementById("cn-ssh-pass").value;
    if (!password) {
      throw new Error(`Вход по ключу не работает: ${result.detail}. Впишите пароль сервера — `
        + "ключ поставится автоматически.");
    }
    connectStatus.textContent = "Ставлю ключ доступа на сервер…";
    result = await api("/api/v1/access/ssh/install-key", {
      method: "POST", body: JSON.stringify({ target, password }),
    });
    document.getElementById("cn-ssh-pass").value = "";
  }
  if (!result.ok) throw new Error(`Сервер не пускает: ${result.detail}`);
  connectStatus.textContent = "Вход на сервер объекта работает.";
  return target;
}

document.getElementById("cn-ssh-check").onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  connectError.hidden = true;
  try {
    await ensureSshKey();
  } catch (error) {
    connectFail(error);
  } finally {
    button.disabled = false;
  }
};

connectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const go = document.getElementById("connect-go");
  go.disabled = true;
  connectError.hidden = true;
  const name = connectForm.name.value.trim();
  const kind = connectKind();
  let subnet = "";
  try {
    // Название проверяем ДО импорта VPN: иначе VPN остался бы в системе, а
    // объект не создался бы из-за дубля имени.
    if (siteNames().some((n) => n.toLowerCase() === name.toLowerCase())) {
      throw new Error(`Объект «${name}» уже есть`);
    }
    if (kind === "wireguard") {
      const file = document.getElementById("cn-wg-file").files[0];
      if (!file) throw new Error("Выберите файл .conf");
      // Имя VPN - имя сетевого интерфейса: латиница, уникальное. Из
      // кириллического названия его не вывести - общий запасной вариант
      // давал бы всем объектам одно имя, и второй VPN клал бы первый.
      const latin = name.toLowerCase().replace(/[^a-z0-9_-]+/g, "").replace(/^[^a-z]+/, "").slice(0, 15);
      const vpnName = document.getElementById("cn-wg-name").value.trim() || latin;
      if (!vpnName) throw new Error("Впишите имя VPN латиницей, например karakol");
      connectStatus.textContent = "Импортирую VPN…";
      const result = await api("/api/v1/access/wireguard", {
        method: "POST", body: JSON.stringify({ name: vpnName, conf: await file.text() }),
      });
      if (!result.ok) throw new Error(`VPN импортирован, но не поднялся: ${result.detail}`);
      subnet = (result.networks || []).find((n) => /\/2[2-9]$|\/3[0-2]$/.test(n)) || "";
    }
    let sshTarget = "";
    if (kind === "ssh") sshTarget = await ensureSshKey();

    const body = { name };
    if (kind === "socks") body.proxy = document.getElementById("cn-socks").value.trim();
    await api("/api/v1/sites", { method: "POST", body: JSON.stringify(body) });
    if (kind === "ssh") {
      await api(`/api/v1/sites/${encodeURIComponent(name)}/access`, {
        method: "PUT", body: JSON.stringify({ kind: "ssh", target: sshTarget }),
      });
    }
    connectDialog.close();
    await refresh();
    goTo(name, null);
    // Сразу - поиск оборудования: подсеть из VPN, если она известна.
    await openScan(name);
    if (subnet) scanForm.subnet.value = subnet;
    if (kind === "ssh") {
      scanEls.status.textContent = "Туннель поднимается (несколько секунд). Укажите подсеть "
        + "объекта и нажмите «Сканировать».";
    }
  } catch (error) {
    connectFail(error);
  } finally {
    go.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// поиск оборудования (скан подсети точки)
// ---------------------------------------------------------------------------

const scanDialog = document.getElementById("scan-dialog");
const scanForm = document.getElementById("scan-form");
const scanEls = {
  status: document.getElementById("scan-status"),
  progress: document.getElementById("scan-progress"),
  results: document.getElementById("scan-results"),
  error: document.getElementById("scan-error"),
  ai: document.getElementById("scan-ai"),
  add: document.getElementById("scan-add"),
  checkNew: document.getElementById("scan-check-new"),
};
let scanSite = null;
let scanTimer = null;
let scanRows = [];   // [{item, check, name, category}]
// Какие из найденных портов проверять у нового устройства: веб и поток важнее.
const MONITOR_PORT_ORDER = [80, 554, 37777, 443, 8000, 8080, 22, 10050, 23];

function scanFail(error) {
  scanEls.error.textContent = error.message;
  scanEls.error.hidden = false;
}

async function openScan(site) {
  scanSite = site;
  clearInterval(scanTimer);
  document.getElementById("scan-start").disabled = false;
  document.getElementById("scan-title").textContent = `Найти оборудование — ${site}`;
  scanEls.results.textContent = "";
  scanEls.status.textContent = "";
  scanEls.error.hidden = true;
  scanEls.progress.hidden = true;
  [scanEls.ai, scanEls.add, scanEls.checkNew].forEach((b) => { b.hidden = true; });
  scanRows = [];
  try {
    const { subnet } = await api(`/api/v1/sites/${encodeURIComponent(site)}/scan`);
    scanForm.subnet.value = subnet || "";
  } catch (error) {
    scanFail(error);
  }
  scanDialog.showModal();
}

scanForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  scanEls.error.hidden = true;
  scanEls.results.textContent = "";
  try {
    const { job } = await api(`/api/v1/sites/${encodeURIComponent(scanSite)}/scan`, {
      method: "POST", body: JSON.stringify({ subnet: scanForm.subnet.value }),
    });
    document.getElementById("scan-start").disabled = true;
    scanEls.progress.hidden = false;
    pollScan(job.id);
  } catch (error) {
    scanFail(error);
  }
});

function pollScan(jobId) {
  clearInterval(scanTimer);
  const tick = async () => {
    let job;
    try {
      ({ job } = await api(`/api/v1/scans/${jobId}`));
    } catch (error) {
      clearInterval(scanTimer);
      scanFail(error);
      document.getElementById("scan-start").disabled = false;
      return;
    }
    scanEls.progress.max = job.total || 1;
    scanEls.progress.value = job.done;
    scanEls.status.textContent = job.status === "running"
      ? `Проверено ${job.done} из ${job.total} адресов… через туннель это несколько минут.`
      : "";
    if (job.status === "running") return;
    clearInterval(scanTimer);
    document.getElementById("scan-start").disabled = false;
    scanEls.progress.hidden = true;
    if (job.status === "error") scanFail(new Error(job.error));
    else renderScanResults(job);
  };
  tick();
  scanTimer = setInterval(tick, 1500);
}

function renderScanResults(job) {
  const fresh = job.results.filter((r) => !r.existing);
  scanEls.status.textContent = `Найдено ${job.results.length}: новых ${fresh.length}, `
    + `уже в панели ${job.results.length - fresh.length}.`;
  const table = document.createElement("table");
  table.className = "scan-table";
  table.innerHTML = "<thead><tr><th></th><th>Адрес</th><th>Порты</th><th>Что это</th>"
    + "<th>Имя</th><th>Категория</th><th></th></tr></thead>";
  const body = document.createElement("tbody");
  scanRows = job.results.map((item) => {
    const tr = document.createElement("tr");
    const cell = (node) => { const td = document.createElement("td"); td.append(node); tr.append(td); return td; };
    const check = document.createElement("input");
    check.type = "checkbox";
    check.disabled = !!item.existing;
    const name = document.createElement("input");
    name.value = item.existing ? item.existing.name : item.name;
    name.disabled = !!item.existing;
    const category = document.createElement("input");
    category.value = item.category;
    category.setAttribute("list", "category-options");
    category.disabled = !!item.existing;
    const what = [item.vendor, item.model, item.title].filter(Boolean).join(" · ") || "—";
    cell(check);
    cell(document.createTextNode(item.host));
    cell(document.createTextNode(item.ports.join(", ")));
    cell(document.createTextNode(what));
    cell(name);
    cell(category);
    const status = document.createElement("span");
    status.className = item.existing ? "scan-known" : "scan-new";
    status.textContent = item.existing ? "уже есть" : "новое";
    cell(status);
    body.append(tr);
    return { item, check, name, category, status };
  });
  table.append(body);
  scanEls.results.textContent = "";
  scanEls.results.append(table);
  if (job.missing.length) {
    const missing = document.createElement("p");
    missing.className = "hint scan-missing";
    missing.textContent = "Заведены в панели, но в скане не ответили: "
      + job.missing.map((m) => `${m.name} (${m.host})`).join(", ");
    scanEls.results.append(missing);
  }
  scanEls.add.hidden = scanEls.checkNew.hidden = !fresh.length;
  scanEls.ai.hidden = !fresh.length || !state.assistant_configured;
}

scanEls.checkNew.onclick = () => scanRows.forEach((r) => { if (!r.check.disabled) r.check.checked = true; });

scanEls.ai.onclick = async () => {
  const rows = scanRows.filter((r) => !r.check.disabled);
  scanEls.ai.disabled = true;
  scanEls.error.hidden = true;
  try {
    const { suggestions } = await api("/api/v1/assistant/names", {
      method: "POST",
      body: JSON.stringify({ candidates: rows.map((r) => ({ ...r.item, name: r.name.value })) }),
    });
    const byHost = new Map(suggestions.map((s) => [s.host, s]));
    rows.forEach((r) => {
      const s = byHost.get(r.item.host);
      if (!s) return;
      if (s.name) r.name.value = s.name;
      if (s.category) r.category.value = s.category;
    });
    scanEls.status.textContent += " Имена предложены ИИ — проверьте перед добавлением.";
  } catch (error) {
    scanFail(error);
  } finally {
    scanEls.ai.disabled = false;
  }
};

scanEls.add.onclick = async () => {
  const chosen = scanRows.filter((r) => r.check.checked && !r.check.disabled);
  if (!chosen.length) {
    scanFail(new Error("Ничего не отмечено"));
    return;
  }
  scanEls.add.disabled = true;
  scanEls.error.hidden = true;
  let added = 0;
  const errors = [];
  for (const r of chosen) {
    const ports = MONITOR_PORT_ORDER.filter((p) => r.item.ports.includes(p)).slice(0, 3);
    try {
      await api("/api/v1/devices", {
        method: "POST",
        body: JSON.stringify({
          site: scanSite, category: r.category.value.trim(), name: r.name.value.trim(),
          host: r.item.host, ports: ports.length ? ports : r.item.ports.slice(0, 3),
          web_port: r.item.web_port && r.item.web_port !== 80 ? r.item.web_port : null,
        }),
      });
      added += 1;
      r.check.checked = false;
      r.check.disabled = r.name.disabled = r.category.disabled = true;
      r.status.className = "scan-known";
      r.status.textContent = "добавлено";
    } catch (error) {
      errors.push(`${r.item.host}: ${error.message}`);
    }
  }
  scanEls.add.disabled = false;
  scanEls.status.textContent = `Добавлено ${added}.` + (errors.length ? ` Ошибки: ${errors.join("; ")}` : "");
  await refresh();
};

document.getElementById("scan-close").onclick = () => scanDialog.close();
// Закрытие любым способом (кнопка, Esc) останавливает опрос прогресса; сам
// скан на сервере доработает, а повторное открытие начнёт с чистого листа.
scanDialog.addEventListener("close", () => {
  clearInterval(scanTimer);
  document.getElementById("scan-start").disabled = false;
});

// ---------------------------------------------------------------------------
// ИИ-помощник: настройки провайдера
// ---------------------------------------------------------------------------

const assistantEditor = document.getElementById("assistant-editor");
const assistantForm = document.getElementById("assistant-form");
const assistantError = document.getElementById("assistant-error");

function fillAssistant(s) {
  assistantForm.base_url.value = s.base_url || "";
  assistantForm.model.value = s.model || "";
  assistantForm.api_key.value = "";
  assistantForm.api_key.placeholder = s.has_key ? "сохранён — пусто = не менять" : "";
  assistantForm.enabled.checked = s.enabled;
}

function assistantPayload() {
  const payload = {
    base_url: assistantForm.base_url.value.trim(),
    model: assistantForm.model.value.trim(),
    enabled: assistantForm.enabled.checked,
  };
  if (assistantForm.api_key.value.trim()) payload.api_key = assistantForm.api_key.value.trim();
  return payload;
}

document.getElementById("assistant").onclick = async () => {
  assistantError.hidden = true;
  document.getElementById("as-status").textContent = "";
  try {
    fillAssistant(await api("/api/v1/assistant"));
  } catch (error) {
    assistantError.textContent = error.message;
    assistantError.hidden = false;
  }
  assistantEditor.showModal();
};
document.getElementById("assistant-cancel").onclick = () => assistantEditor.close();
document.getElementById("as-test").onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  assistantError.hidden = true;
  try {
    fillAssistant(await api("/api/v1/assistant", { method: "PUT", body: JSON.stringify(assistantPayload()) }));
    await api("/api/v1/assistant/test", { method: "POST" });
    document.getElementById("as-status").textContent = "Модель отвечает.";
  } catch (error) {
    assistantError.textContent = error.message;
    assistantError.hidden = false;
  } finally {
    button.disabled = false;
  }
};
assistantForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/v1/assistant", { method: "PUT", body: JSON.stringify(assistantPayload()) });
    assistantEditor.close();
    await refresh();
  } catch (error) {
    assistantError.textContent = error.message;
    assistantError.hidden = false;
  }
});

const accountEditor = document.getElementById("account-editor");
const accountForm = document.getElementById("account-form");
const accountError = document.getElementById("account-error");

// Шаги пустого экрана ведут на те же действия, что и кнопки панели.
document.getElementById("import-first").onclick = () => document.getElementById("import").click();
document.getElementById("alerts-first").onclick = () => document.getElementById("alerts").click();

document.getElementById("account").onclick = async () => {
  accountError.hidden = true;
  accountForm.reset();
  accountForm.new_password.type = "password";
  try {
    const s = await api("/api/v1/session");
    accountForm.username.value = s.username || "";
    // Предупредить, если вход всё ещё под дефолтным паролем публичного репо.
    document.getElementById("account-warn").textContent =
      "Панель хранит доступы к оборудованию — держите пароль надёжным и не дефолтным.";
  } catch (e) {}
  accountEditor.showModal();
};

document.getElementById("account-cancel").onclick = () => accountEditor.close();

// глаз для нового пароля в форме аккаунта
const aEye = document.getElementById("a-new-eye");
if (aEye) {
  aEye.innerHTML = EYE;
  aEye.onclick = () => {
    const f = accountForm.new_password;
    const shown = f.type === "text";
    f.type = shown ? "password" : "text";
    aEye.innerHTML = shown ? EYE : EYE_OFF;
  };
}

accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = { current_password: accountForm.current_password.value };
  if (accountForm.username.value) payload.username = accountForm.username.value;
  if (accountForm.new_password.value) payload.new_password = accountForm.new_password.value;
  try {
    await api("/api/v1/account", { method: "POST", body: JSON.stringify(payload) });
    accountEditor.close();
    showAlert("");
    // Смена логина/пароля не рвёт текущую сессию, но освежим на всякий случай.
    await refresh();
  } catch (error) {
    accountError.textContent = error.message;
    accountError.hidden = false;
  }
});

document.getElementById("recheck").onclick = async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Проверяю…";
  try {
    await api("/api/v1/recheck", { method: "POST" });
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

setupViewToggle();
refresh();
