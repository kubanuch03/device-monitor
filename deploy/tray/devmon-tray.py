#!/usr/bin/env python3
"""Значок Device Monitor в трее Ubuntu + уведомления рабочего стола.

Опрашивает /api/v1/tray раз в 30 с с ключом из data/tray-token (не паролем
панели: трею нужна только сводка «кто лежит»). Цвет значка - по состоянию,
в меню - кто не отвечает, клик открывает панель. Уведомление Ubuntu - когда
устройство лежит дольше окна подтверждения (то же правило, что у Telegram,
чтобы не мигать на каждый сбой) и когда оно вернулось.

Запускается системным python3: ему нужны PyGObject и Gtk из пакетов Ubuntu,
в venv сервиса их нет. Без пакета gir1.2-ayatanaappindicator3-0.1 значка не
будет, но уведомления работают (режим «только уведомления»).

Настройка через окружение (ставит deploy/install-ubuntu.sh):
  DM_TRAY_URL         адрес панели, по умолчанию http://127.0.0.1:8890
  DM_TRAY_TOKEN_FILE  путь к ключу, по умолчанию ~/.local/share/device-monitor/data/tray-token
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import traceback
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import GLib, Gtk, Notify  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    AppIndicator = None

URL = os.environ.get("DM_TRAY_URL", "http://127.0.0.1:8890").rstrip("/")
TOKEN_FILE = Path(os.environ.get(
    "DM_TRAY_TOKEN_FILE",
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "device-monitor/data/tray-token",
))
POLL_SECONDS = 30
MENU_LIMIT = 15

ICONS = {"ok": "emblem-default", "down": "dialog-warning", "offline": "network-offline"}


def fetch() -> dict:
    token = TOKEN_FILE.read_text("utf-8").strip()
    request = urllib.request.Request(f"{URL}/api/v1/tray", headers={"X-Tray-Token": token})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def open_panel(hash_: str = "") -> None:
    subprocess.Popen(["xdg-open", f"{URL}/{hash_}"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


class Tray:
    def __init__(self):
        Notify.init("Device Monitor")
        self.notified_down: set[str] = set()   # о чьём падении уже сказали
        self.first = True
        self.indicator = None
        if AppIndicator is not None:
            self.indicator = AppIndicator.Indicator.new(
                "device-monitor", ICONS["offline"], AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("Device Monitor")
        self.render(None, "ещё не опрошено")

    # -- меню ------------------------------------------------------------------

    def render(self, data: dict | None, error: str = "") -> None:
        if self.indicator is None:
            return
        menu = Gtk.Menu()

        def item(label, handler=None, sensitive=True):
            entry = Gtk.MenuItem(label=label)
            entry.set_sensitive(sensitive)
            if handler:
                entry.connect("activate", lambda *_: handler())
            menu.append(entry)

        if data is None:
            self.indicator.set_icon_full(ICONS["offline"], "нет связи с панелью")
            item(f"Нет связи с панелью: {error}", sensitive=False)
        else:
            down = data["down"]
            loud = [d for d in down if not d["muted"]]
            self.indicator.set_icon_full(ICONS["down" if loud else "ok"],
                                         f"не отвечают {len(down)}")
            item(f"На связи {data['up']} из {data['total']}", sensitive=False)
            if down:
                item(f"Не отвечают: {len(down)}", lambda: open_panel("#down=1"))
                for device in down[:MENU_LIMIT]:
                    mark = "🔕 " if device["muted"] else ""
                    item(f"   {mark}{device['site']} · {device['name']}",
                         lambda: open_panel("#down=1"))
                if len(down) > MENU_LIMIT:
                    item(f"   … ещё {len(down) - MENU_LIMIT}", lambda: open_panel("#down=1"))
        menu.append(Gtk.SeparatorMenuItem())
        item("Открыть панель", open_panel)
        item("Выход", Gtk.main_quit)
        menu.show_all()
        self.indicator.set_menu(menu)

    # -- уведомления -------------------------------------------------------------

    @staticmethod
    def _lasting(device: dict, now: datetime) -> float:
        try:
            return (now - datetime.fromisoformat(device["since"])).total_seconds()
        except (TypeError, ValueError):
            return 0

    def notify(self, data: dict) -> None:
        now = datetime.now(timezone.utc)
        confirmed = {d["id"]: d for d in data["down"]
                     if not d["muted"] and self._lasting(d, now) >= data["confirm_seconds"]}
        if self.first:
            # Что лежало до запуска значка - не новость, иначе при каждом входе
            # в систему всплывала бы пачка уведомлений.
            self.notified_down = set(confirmed)
            self.first = False
            return
        fresh = [d for i, d in confirmed.items() if i not in self.notified_down]
        current_down = {d["id"] for d in data["down"]}
        back = self.notified_down - current_down
        if fresh:
            body = "\n".join(f"{d['site']} · {d['name']} ({d['host']})" for d in fresh[:8])
            if len(fresh) > 8:
                body += f"\n… и ещё {len(fresh) - 8}"
            Notify.Notification.new(f"Не отвечают: {len(fresh)}", body, "dialog-warning").show()
        if back:
            Notify.Notification.new(f"Снова на связи: {len(back)}", "", "emblem-default").show()
        self.notified_down = (self.notified_down & current_down) | set(confirmed)

    # -- цикл --------------------------------------------------------------------

    def poll(self) -> bool:
        # Любая ошибка - в лог, но таймер живёт: исключение из колбэка GLib
        # снимает таймер, и значок замер бы в последнем состоянии.
        try:
            try:
                data = fetch()
            except Exception as exc:   # панель выключена, ключа нет, сеть
                self.render(None, type(exc).__name__)
                return True
            if not data.get("ready", True):
                # Сервис только что перезапущен, круга ещё не было: пустой
                # список упавших значит «не знаем», а не «все вернулись».
                return True
            self.render(data)
            self.notify(data)
        except Exception:
            traceback.print_exc()
        return True   # GLib: продолжать таймер


def single_instance():
    """Второй экземпляр (повторный запуск установщика, автозапуск поверх уже
    запущенного) молча выходит: иначе два значка и двойные уведомления."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    handle = open(runtime / "devmon-tray.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)
    return handle   # держать открытым до конца процесса


def main() -> None:
    lock = single_instance()  # noqa: F841
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    tray = Tray()
    tray.poll()
    GLib.timeout_add_seconds(POLL_SECONDS, tray.poll)
    if AppIndicator is None:
        print("[tray] нет пакета gir1.2-ayatanaappindicator3-0.1 - работают только уведомления",
              flush=True)
    Gtk.main()


if __name__ == "__main__":
    main()
