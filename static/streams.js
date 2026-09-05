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

function showAlert(text) {
  alertBox.textContent = text;
  alertBox.hidden = !text;
}

// go2rtc отдаёт встраиваемый плеер по /stream.html. mode=webrtc даёт
// наименьшую задержку; если у камеры не сойдётся кодек, go2rtc сам
// откатится на mse внутри своего плеера.
function playerUrl(base, name) {
  return `${base}/stream.html?src=${encodeURIComponent(name)}&mode=webrtc`;
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
  head.append(name, state);

  const frame = document.createElement("iframe");
  frame.className = "stream-frame";
  frame.src = playerUrl(base, device.stream_name);
  frame.allow = "autoplay; fullscreen";
  frame.loading = "lazy";

  const foot = document.createElement("div");
  foot.className = "stream-foot";
  const src = document.createElement("span");
  src.textContent = `${device.stream_name} · ${device.host}`;
  const alone = document.createElement("a");
  alone.href = `/static/streams.html?site=${encodeURIComponent(device.site)}&device=${encodeURIComponent(device.id)}`;
  alone.target = "_blank";
  alone.rel = "noopener";
  alone.textContent = "Открыть отдельно ↗";
  const direct = document.createElement("a");
  direct.href = playerUrl(base, device.stream_name);
  direct.target = "_blank";
  direct.rel = "noopener";
  direct.textContent = "Плеер go2rtc ↗";
  foot.append(src, alone, direct);

  box.append(head, frame, foot);
  return box;
}

async function load() {
  if (!site) { showAlert("Не указана точка."); return; }
  let state;
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error(`Ошибка ${response.status}`);
    state = await response.json();
  } catch (error) {
    showAlert(`Сервис не отвечает: ${error.message}`);
    return;
  }

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

  grid.textContent = "";
  cams.forEach((d) => grid.append(tile(info.stream_base, d)));
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

// Перезапуск = пересоздание iframe. Нужен, когда туннель моргнул и плеер
// повис: go2rtc сам переподключится, но уже мёртвый iframe не оживёт.
document.getElementById("s-reload").onclick = () => load();
document.getElementById("s-back").onclick = () => {
  location.href = `/#site=${encodeURIComponent(site || "")}`;
};

load();
