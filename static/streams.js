"use strict";

// Просмотр видео точки. Своей обработки RTSP здесь нет и быть не может:
// браузер этот протокол не понимает. Поток переупаковывает go2rtc, который
// уже стоит на серверах объектов, а эта страница только раскладывает его
// плееры сеткой. Поэтому у сервиса по-прежнему ноль зависимостей.
//
// Адрес go2rtc задаётся у точки (stream_base), имя источника — у устройства
// (stream_name). Устройства без имени источника сюда не попадают: роутеру и
// коммутатору видео не положено.

const params = new URLSearchParams(location.search);
const site = params.get("site");
const only = params.get("device");

const grid = document.getElementById("s-grid");
const alertBox = document.getElementById("s-alert");
const titleEl = document.getElementById("s-title");
const subEl = document.getElementById("s-sub");
const colsSel = document.getElementById("s-cols");
const qualitySel = document.getElementById("s-quality");

// Качество: HD - основной источник камеры, SD - источник `<имя>_sd` в go2rtc
// (подпоток камеры). Сетке по умолчанию SD: десяток основных потоков по 4 Мп
// через VPN забивает канал, а в маленькой плитке разницы не видно. Одной
// камере - HD. Выбор запоминается отдельно для сетки и для одной камеры.
const qualityKey = only ? "dm_stream_quality_one" : "dm_stream_quality_grid";
qualitySel.value = only ? "hd" : "sd";
try {
  const saved = localStorage.getItem(qualityKey);
  if (saved) qualitySel.value = saved;
} catch (e) { /* приватный режим */ }

// Какие источники есть в go2rtc каждой точки: SD есть не у всех камер.
// Спрашиваем через сервис - у go2rtc нет CORS. Кэш на время жизни страницы.
const namesBySite = {};

async function siteNames(name) {
  if (!(name in namesBySite)) {
    try {
      const response = await fetch(`/api/v1/sites/${encodeURIComponent(name)}/streams`);
      namesBySite[name] = new Set(response.ok ? (await response.json()).names : []);
    } catch (error) {
      namesBySite[name] = new Set();
    }
  }
  return namesBySite[name];
}

// Источник под выбранное качество. Без пары `_sd` у камеры один источник, и
// какого он качества, отсюда не видно (у Балыкчы, например, go2rtc объекта
// отдаёт только 704×576) - поэтому подписываем «один поток», а не «HD».
function source(device) {
  const sd = `${device.stream_name}_sd`;
  const names = namesBySite[device.site] || new Set();
  if (!names.has(sd)) return { name: device.stream_name, label: "один поток" };
  return qualitySel.value === "sd"
    ? { name: sd, label: "SD" }
    : { name: device.stream_name, label: "HD" };
}

function showAlert(text) {
  alertBox.textContent = text;
  alertBox.hidden = !text;
}

// go2rtc отдаёт встраиваемый плеер по /stream.html. WebRTC даёт наименьшую
// задержку, но медиа идёт по UDP, и до go2rtc на удалённом объекте (через
// VPN/NAT) он не пробивается: плеер вечно висит в «loading». MSE идёт через
// тот же HTTP-порт по websocket и проходит через любой туннель, поэтому он
// запасной. Плеер go2rtc пробует режимы по очереди.
function playerUrl(base, name) {
  return `${base}/stream.html?src=${encodeURIComponent(name)}&mode=webrtc,mse`;
}

function tile(base, device) {
  const box = document.createElement("div");
  box.className = "stream-tile";

  const head = document.createElement("div");
  head.className = "stream-head";
  const name = document.createElement("span");
  name.className = "stream-name";
  name.textContent = device.name;
  const state = document.createElement("span");
  state.className = `state ${device.status}`;
  state.textContent = device.status === "up" ? "на связи"
    : device.status === "down" ? "не отвечает" : "не проверено";
  const quality = source(device);
  const badge = document.createElement("span");
  badge.className = "stream-quality";
  badge.textContent = quality.label;
  // IP рядом с именем: по нему камеру ищут в веб-интерфейсе и в регистраторе,
  // а в подвале под плеером его не видно без прокрутки.
  const ip = document.createElement("span");
  ip.className = "stream-ip";
  ip.textContent = device.host;
  head.append(name, ip, badge, state);

  const frame = document.createElement("iframe");
  frame.className = "stream-frame";
  frame.src = playerUrl(base, quality.name);
  frame.allow = "autoplay; fullscreen";
  frame.loading = "lazy";

  const foot = document.createElement("div");
  foot.className = "stream-foot";
  const src = document.createElement("span");
  src.textContent = quality.name;
  const alone = document.createElement("a");
  alone.href = `/static/streams.html?site=${encodeURIComponent(device.site)}&device=${encodeURIComponent(device.id)}`;
  alone.target = "_blank";
  alone.rel = "noopener";
  alone.textContent = "Открыть отдельно ↗";
  const direct = document.createElement("a");
  direct.href = playerUrl(base, quality.name);
  direct.target = "_blank";
  direct.rel = "noopener";
  direct.textContent = "Плеер go2rtc ↗";
  foot.append(src, alone, direct);

  box.append(head, frame, foot);
  return box;
}

async function load() {
  let state;
  try {
    const response = await fetch("/api/v1/state");
    if (!response.ok) throw new Error(`Ошибка ${response.status}`);
    state = await response.json();
  } catch (error) {
    showAlert(`Сервис не отвечает: ${error.message}`);
    return;
  }

  // Без точки в адресе - общий экран: все камеры всех точек, у которых есть
  // и адрес go2rtc, и имя потока. Адрес go2rtc у каждой точки свой, поэтому
  // плитка берёт его от своей точки, а не один на всю страницу.
  if (!site) { loadAll(state); return; }

  const info = (state.sites || []).find((s) => s.name === site);
  if (!info) { showAlert(`Точка «${site}» не найдена.`); return; }

  titleEl.textContent = `Видео — ${site}`;

  if (!info.stream_base) {
    showAlert(
      "У точки не задан адрес go2rtc. Откройте «Настроить точку» и впишите его "
      + "в поле «Видео (go2rtc)» — например http://10.20.4.2:1983. "
      + "Браузер не умеет RTSP сам, поток переупаковывает go2rtc на сервере объекта."
    );
    return;
  }

  let cams = (state.devices || []).filter((d) => d.site === site && d.stream_name);
  if (only) cams = cams.filter((d) => d.id === only);

  if (!cams.length) {
    showAlert(
      only
        ? "Устройство не найдено или у него не задано имя потока."
        : "Ни у одного устройства точки не задано имя потока. Впишите его в поле "
          + "«Имя потока в go2rtc» в настройках камеры — так сервис узнаёт, какой "
          + "источник показывать."
    );
    return;
  }

  const down = cams.filter((d) => d.status === "down").length;
  subEl.innerHTML = `<span>камер <b>${cams.length}</b></span>`
    + (down ? `<span class="s-down">не отвечают <b>${down}</b></span>` : "");
  if (down) {
    showAlert(
      `Не отвечают ${down} из ${cams.length}. Плеер всё равно показан — видео идёт `
      + "через go2rtc, а не напрямую, поэтому оно может работать даже когда до "
      + "камеры нет прямого доступа с этой машины."
    );
  }

  await siteNames(site);
  grid.textContent = "";
  cams.forEach((d) => grid.append(tile(info.stream_base, d)));
  applyCols();
}

async function loadAll(state) {
  titleEl.textContent = "Видео — все камеры";
  const bases = Object.fromEntries(
    (state.sites || []).filter((s) => s.stream_base).map((s) => [s.name, s.stream_base]));
  const cams = (state.devices || []).filter((d) => d.stream_name && bases[d.site]);

  if (!cams.length) {
    showAlert("Нет камер с видео: у точки нужен адрес go2rtc, у камеры — имя потока.");
    return;
  }
  const down = cams.filter((d) => d.status === "down").length;
  const sites = new Set(cams.map((d) => d.site)).size;
  subEl.innerHTML = `<span>точек <b>${sites}</b></span><span>камер <b>${cams.length}</b></span>`
    + (down ? `<span class="s-down">не отвечают <b>${down}</b></span>` : "");
  showAlert("");

  await Promise.all([...new Set(cams.map((d) => d.site))].map(siteNames));
  grid.textContent = "";
  cams.forEach((d) => {
    const box = tile(bases[d.site], d);
    // На общем экране имя камеры без точки ничего не говорит: «Въезд 1» есть
    // на нескольких объектах.
    box.querySelector(".stream-name").textContent = `${d.site} · ${d.name}`;
    grid.append(box);
  });
  applyCols();
}

function applyCols() {
  const n = only ? 1 : Number(colsSel.value || 2);
  grid.style.gridTemplateColumns = `repeat(${n}, minmax(0, 1fr))`;
}

colsSel.onchange = () => {
  applyCols();
  try { localStorage.setItem("dm_stream_cols", colsSel.value); } catch (e) { /* приватный режим */ }
};
try {
  const saved = localStorage.getItem("dm_stream_cols");
  if (saved) colsSel.value = saved;
} catch (e) { /* приватный режим */ }

qualitySel.onchange = () => {
  try { localStorage.setItem(qualityKey, qualitySel.value); } catch (e) { /* приватный режим */ }
  load();
};

// Перезапуск = пересоздание iframe. Нужен, когда туннель моргнул и плеер
// повис: go2rtc сам переподключится, но уже мёртвый iframe не оживёт.
document.getElementById("s-reload").onclick = () => load();
document.getElementById("s-back").onclick = () => {
  location.href = site ? `/#site=${encodeURIComponent(site)}` : "/";
};

load();
