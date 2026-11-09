"""Оповещения: настройки, окна обслуживания, отправка в фоне.

Отправка идёт отдельным потоком через очередь: медленный или недоступный
Telegram не должен задерживать круг опроса - иначе пропадание интернета
останавливало бы и сам мониторинг.
"""

from __future__ import annotations

import queue
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from device_monitor import config
from device_monitor.alerts import telegram
from device_monitor.alerts.rules import AlertEngine, AlertRules, Mutes, Observation, render
from device_monitor.domain.errors import ValidationError
from device_monitor.security import deobfuscate, obfuscate

SETTINGS_KEY = "alerts"
MUTES_KEY = "mutes"

DEFAULTS = {
    "enabled": True,
    "telegram_token_enc": "",
    "telegram_chat_id": "",
    "telegram_chat_title": "",
    "down_confirm": 3,
    "site_outage_ratio": 0.7,
    "repeat_hours": 0,
    "heartbeat_url": "",
}


def _tz():
    try:
        return ZoneInfo(config.TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone(timedelta(hours=6))  # Кыргызстан, без перехода на летнее


class _Sender:
    """Фоновая отправка с повтором. Очередь ограничена: при долгом отсутствии
    интернета старые сообщения вытесняются, а не копятся без предела."""

    RETRY_DELAYS = (2, 10, 30)

    def __init__(self):
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_ok: str | None = None       # только Telegram: статус сторожа
        self.last_error: str | None = None    # ведётся отдельно (Heartbeat)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="alerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # будим поток, ждущий в get()
        except queue.Full:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def submit(self, job: Callable[[], None], what: str) -> None:
        try:
            self._queue.put_nowait((job, what))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait((job, what))

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            job, what = item
            for attempt, delay in enumerate((0, *self.RETRY_DELAYS)):
                if delay and self._stop.wait(delay):
                    return
                try:
                    job()
                    self.last_ok = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self.last_error = None
                    break
                except Exception as exc:
                    self.last_error = str(exc)
                    if attempt == len(self.RETRY_DELAYS):
                        print(f"[alerts] не отправлено ({what}): {exc}", flush=True)


class _Heartbeat:
    """Пинг сторожа - отдельно от очереди Telegram и без повторов.

    В общей очереди мёртвый адрес сторожа с повторами занимал её минутами и
    вытеснял настоящие тревоги. Здесь не больше одного запроса в полёте:
    пока предыдущий не кончился, новый круг пинг просто пропускает - сторожу
    важен факт регулярных пингов, а не каждый из них."""

    def __init__(self):
        self._busy = threading.Lock()
        self.last_ok: str | None = None
        self.last_error: str | None = None

    def ping(self, url: str) -> None:
        if not self._busy.acquire(blocking=False):
            return

        def run():
            try:
                urllib.request.urlopen(url, timeout=5).close()
                self.last_ok = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                self._busy.release()

        threading.Thread(target=run, name="heartbeat", daemon=True).start()


class AlertService:
    BOT_NAME_TTL = 300  # сколько помнить, что getMe не ответил

    def __init__(self, storage, run_sender: bool = True):
        self.storage = storage
        self.engine = AlertEngine(AlertRules(), self._baseline, tz=_tz())
        self.sender = _Sender()
        self.heartbeat = _Heartbeat()
        self._run_sender = run_sender
        # (token, @имя или "", когда спрашивали): неудачу тоже помним, иначе
        # без интернета каждый GET настроек ждал бы таймаут getMe.
        self._bot_cache: tuple[str, str, float] | None = None

    def _baseline(self, device_id: str) -> tuple[str | None, str | None]:
        last = self.storage.history(device_id, 1)
        return (last[0]["status"], last[0]["at"]) if last else (None, None)

    def start(self) -> None:
        if self._run_sender:
            self.sender.start()

    def stop(self) -> None:
        self.sender.stop()

    # -- настройки ---------------------------------------------------------------

    def _settings(self) -> dict:
        stored = self.storage.get_setting(SETTINGS_KEY)
        merged = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
        return merged

    def _token(self, settings: dict) -> str:
        return deobfuscate(settings["telegram_token_enc"]) if settings["telegram_token_enc"] \
            else config.TG_TOKEN

    def _chat(self, settings: dict) -> str:
        return settings["telegram_chat_id"] or config.TG_CHAT

    def _rules(self, settings: dict) -> AlertRules:
        return AlertRules(
            down_confirm=int(settings["down_confirm"]),
            site_outage_ratio=float(settings["site_outage_ratio"]),
            repeat_hours=float(settings["repeat_hours"]),
        )

    def public_settings(self) -> dict:
        settings = self._settings()
        token = self._token(settings)
        return {
            "enabled": bool(settings["enabled"]),
            "has_token": bool(token),
            "token_from_env": bool(token) and not settings["telegram_token_enc"],
            "bot": self._bot(token),
            "chat_id": self._chat(settings),
            "chat_title": settings["telegram_chat_title"],
            "down_confirm": int(settings["down_confirm"]),
            "site_outage_ratio": float(settings["site_outage_ratio"]),
            "repeat_hours": float(settings["repeat_hours"]),
            "heartbeat_url": settings["heartbeat_url"],
            "poll_interval": config.POLL_INTERVAL,
            "last_ok": self.sender.last_ok,
            "last_error": self.sender.last_error,
            "heartbeat_ok": self.heartbeat.last_ok,
            "heartbeat_error": self.heartbeat.last_error,
        }

    def rules_summary(self) -> dict:
        """Правила без запроса в Telegram (для частых запросов трея)."""
        settings = self._settings()
        return {"down_confirm": int(settings["down_confirm"])}

    def configured(self) -> bool:
        """Дёшево, без запроса в Telegram: дёргается на каждый /state."""
        settings = self._settings()
        return bool(settings["enabled"] and self._token(settings) and self._chat(settings))

    def _bot(self, token: str) -> str:
        if not token:
            return ""
        cached = self._bot_cache
        if cached and cached[0] == token and (cached[1] or time.time() - cached[2] < self.BOT_NAME_TTL):
            return cached[1]
        try:
            name = telegram.bot_name(token)
        except telegram.TelegramError:
            name = ""
        self._bot_cache = (token, name, time.time())
        return name

    def update_settings(self, payload: dict) -> dict:
        settings = self._settings()
        if "telegram_token" in payload:
            raw = payload["telegram_token"]
            if raw is False:
                settings["telegram_token_enc"] = ""
            elif raw:
                token = str(raw).strip()
                if not telegram.TOKEN_RE.fullmatch(token):
                    raise ValidationError("Токен бота выглядит не так: 123456789:AA... "
                                          "(выдаёт @BotFather)")
                settings["telegram_token_enc"] = obfuscate(token)
        if "chat_id" in payload:
            chat = str(payload["chat_id"] or "").strip()
            if chat and not chat.lstrip("-").isdigit() and not chat.startswith("@"):
                raise ValidationError("ID чата - число (у групп с минусом) или @имя канала")
            settings["telegram_chat_id"] = chat
            settings["telegram_chat_title"] = str(payload.get("chat_title") or "")[:120]
        if "enabled" in payload:
            settings["enabled"] = bool(payload["enabled"])
        if "down_confirm" in payload:
            settings["down_confirm"] = _bounded_int(payload["down_confirm"], 1, 20, "Подтверждение")
        if "site_outage_ratio" in payload:
            ratio = _bounded_float(payload["site_outage_ratio"], 0.3, 1.0, "Доля упавших")
            settings["site_outage_ratio"] = ratio
        if "repeat_hours" in payload:
            settings["repeat_hours"] = _bounded_float(payload["repeat_hours"], 0, 168, "Повтор")
        if "heartbeat_url" in payload:
            url = str(payload["heartbeat_url"] or "").strip()
            if url and not url.startswith(("https://", "http://")):
                raise ValidationError("Адрес сторожа должен начинаться с http:// или https://")
            settings["heartbeat_url"] = url[:300]
        self.storage.set_setting(SETTINGS_KEY, settings)
        return self.public_settings()

    # -- окна обслуживания --------------------------------------------------------

    def _mutes(self) -> Mutes:
        raw = self.storage.get_setting(MUTES_KEY)
        until = {}
        for key, value in raw.items():
            try:
                until[key] = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
        return Mutes(until)

    def mutes_public(self, now: datetime | None = None) -> dict[str, str]:
        now = now or datetime.now(timezone.utc)
        return {k: v.isoformat(timespec="seconds") for k, v in self._mutes().until.items() if v > now}

    def set_mute(self, kind: str, target: str, minutes: int | None) -> dict[str, str]:
        if kind not in ("site", "device"):
            raise ValidationError("Окно обслуживания ставится на точку или устройство")
        now = datetime.now(timezone.utc)
        active = {k: v for k, v in self._mutes().until.items() if v > now}
        key = f"{kind}:{target}"
        if minutes:
            active[key] = now + timedelta(minutes=_bounded_int(minutes, 1, 60 * 24 * 14, "Длительность"))
        else:
            active.pop(key, None)
        self.storage.set_setting(MUTES_KEY, {k: v.isoformat(timespec="seconds")
                                             for k, v in active.items()})
        return self.mutes_public(now)

    # -- работа -----------------------------------------------------------------

    def process(self, observations: list[Observation], tunnels: dict[str, dict],
                now: datetime | None = None) -> list[str]:
        """Решает, что отправить по итогам круга. Вызывать ДО записи круга в
        историю: движок берёт из неё прежний статус устройства (baseline).
        Возвращает тексты (для тестов); отправка - в фоне."""
        now = now or datetime.now(timezone.utc)
        settings = self._settings()
        self.engine.rules = self._rules(settings)
        mutes = self._mutes()
        token, chat = self._token(settings), self._chat(settings)
        deliverable = bool(settings["enabled"] and token and chat)

        # Пока отправлять некуда, всё учитывается молча, как в окне
        # обслуживания: иначе падение помечалось бы «сообщённым», хотя
        # сообщения не было, а после настройки пришло бы только восстановление.
        def muted(kind: str, target: str) -> bool:
            return not deliverable or mutes.active(kind, target, now)

        texts = render(self.engine.evaluate(observations, tunnels, muted, now))
        for text in texts:
            self.sender.submit(lambda t=text: telegram.send_message(token, chat, t), "Telegram")
        return texts

    def tick(self) -> None:
        """Каждый круг, даже без устройств: сторож должен видеть, что монитор жив."""
        url = self._settings()["heartbeat_url"]
        if url:
            self.heartbeat.ping(url)

    def send_test(self) -> None:
        settings = self._settings()
        token, chat = self._token(settings), self._chat(settings)
        if not token:
            raise ValidationError("Сначала впишите токен бота")
        if not chat:
            raise ValidationError("Сначала укажите группу (кнопка «Найти группу»)")
        try:
            telegram.send_message(token, chat, "✅ Device Monitor: тестовое сообщение. "
                                               "Оповещения о падениях будут приходить сюда.")
        except telegram.TelegramError as exc:
            raise ValidationError(f"Telegram: {exc}") from None

    def find_chats(self) -> list[dict]:
        token = self._token(self._settings())
        if not token:
            raise ValidationError("Сначала впишите токен бота")
        try:
            return telegram.find_chats(token)
        except telegram.TelegramError as exc:
            raise ValidationError(f"Telegram: {exc}") from None


def _bounded_int(value, low: int, high: int, what: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{what}: нужно целое число") from None
    if not low <= number <= high:
        raise ValidationError(f"{what}: от {low} до {high}")
    return number


def _bounded_float(value, low: float, high: float, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{what}: нужно число") from None
    if not low <= number <= high:
        raise ValidationError(f"{what}: от {low} до {high}")
    return number
