#!/usr/bin/env bash
# Установка Device Monitor на Ubuntu как обычного приложения пользователя.
#
#   git clone https://github.com/kubanuch03/device-monitor.git && cd device-monitor
#   deploy/install-ubuntu.sh            # без sudo
#
# Что ставится (всё в домашнем каталоге, root не нужен):
#   - venv с зависимостями:   ~/.local/share/device-monitor/venv
#   - база и ключи:           ~/.local/share/device-monitor/data
#   - сервис:                 systemd --user, device-monitor.service (запуск при входе)
#   - значок в трее:          автозапуск ~/.config/autostart/devmon-tray.desktop
#   - ярлык в меню:           «Device Monitor» (открывает панель в браузере)
#   - обработчик devmon://    для «Открыть» через профиль браузера точки
#
# Параметры:
#   --port N      порт панели (по умолчанию 8890)
#   --no-start    только разложить файлы, сервис не запускать
#
# Видео (go2rtc) и VPN этим скриптом не ставятся: VPN у каждого свой (см.
# deploy/vpn/README.md), go2rtc - по желанию через docker compose.
set -euo pipefail

PORT=8890
START=1
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
    --no-start) START=0; shift ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  echo "Порт должен быть числом 1024–65535, а не «$PORT»" >&2
  exit 2
fi

if [ "$(id -u)" -eq 0 ]; then
  echo "Запускайте без sudo: сервис ставится в домашний каталог пользователя." >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BASE="$DATA_HOME/device-monitor"
VENV="$BASE/venv"
DATA="$BASE/data"

# -- зависимости --------------------------------------------------------------
python3 - <<'EOF' || { echo "Нужен Python 3.11 или новее" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  echo "Нет модуля venv. Поставьте: sudo apt install python3-venv" >&2
  exit 1
fi

mkdir -p "$BASE" "$DATA"
chmod 700 "$BASE" "$DATA"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "Зависимости установлены: $VENV"

# Пути попадают в unit systemd и в .desktop-файлы. Пробел в пути (клон в
# «~/Мои проекты») без кавычек ломает оба формата, а «%» там - спецсимвол.
quote() {
  local v="${1//%/%%}"
  v="${v//\\/\\\\}"
  v="${v//\"/\\\"}"
  printf '"%s"' "$v"
}

# -- сервис -------------------------------------------------------------------
UNIT_DIR="$CONFIG_HOME/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/device-monitor.service" <<EOF
[Unit]
Description=Device Monitor (панель мониторинга оборудования)

[Service]
WorkingDirectory=${APP_DIR//%/%%}
ExecStart=$(quote "$VENV/bin/python") $(quote "$APP_DIR/monitor.py")
Environment=$(quote "DM_DATA_DIR=$DATA")
Environment=DM_HOST=127.0.0.1
Environment=DM_PORT=$PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# -- значок в трее и ярлык ------------------------------------------------------
AUTOSTART="$CONFIG_HOME/autostart"
APPS="$DATA_HOME/applications"
mkdir -p "$AUTOSTART" "$APPS"
cat > "$AUTOSTART/devmon-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Device Monitor — значок
Comment=Значок в трее и уведомления о падениях оборудования
Exec=env DM_TRAY_URL=http://127.0.0.1:$PORT $(quote "DM_TRAY_TOKEN_FILE=$DATA/tray-token") /usr/bin/python3 $(quote "$APP_DIR/deploy/tray/devmon-tray.py")
Icon=network-workgroup
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
cat > "$APPS/device-monitor.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Device Monitor
Comment=Мониторинг камер, домофонов и прочего оборудования объектов
Exec=xdg-open http://127.0.0.1:$PORT/
Icon=network-workgroup
Categories=Network;Monitor;
EOF
update-desktop-database "$APPS" 2>/dev/null || true

if [ -x "$APP_DIR/deploy/install-open-handler.sh" ]; then
  "$APP_DIR/deploy/install-open-handler.sh" >/dev/null 2>&1 \
    && echo "Обработчик devmon:// зарегистрирован" \
    || echo "Обработчик devmon:// не зарегистрирован (необязательно)"
fi

TRAY_OK=1
/usr/bin/python3 - <<'EOF' 2>/dev/null || TRAY_OK=0
import gi
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3
EOF
NOTIFY_OK=1
/usr/bin/python3 - <<'EOF' 2>/dev/null || NOTIFY_OK=0
import gi
gi.require_version("Notify", "0.7")
gi.require_version("Gtk", "3.0")
from gi.repository import Notify, Gtk
EOF

# -- запуск ---------------------------------------------------------------------
if [ "$START" -eq 1 ]; then
  systemctl --user daemon-reload
  # Порт занят чем-то кроме нашего сервиса (например, docker-версией панели):
  # сервис падал бы и молча перезапускался каждые 5 секунд.
  if ! systemctl --user is-active --quiet device-monitor.service \
     && ! python3 -c 'import socket,sys; socket.socket().bind(("127.0.0.1", int(sys.argv[1])))' "$PORT" 2>/dev/null; then
    echo "Порт $PORT уже занят другой программой. Укажите другой: --port 8891" >&2
    exit 1
  fi
  systemctl --user enable device-monitor.service
  # restart, а не enable --now: при обновлении (git pull + этот скрипт) уже
  # работающий сервис иначе так и крутил бы старый код.
  systemctl --user restart device-monitor.service
  ready=0
  for _ in $(seq 1 30); do
    if python3 -c 'import sys,urllib.request; urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/v1/health", timeout=2)' "$PORT" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" -ne 1 ]; then
    echo "Сервис не ответил за 30 с. Журнал: journalctl --user -u device-monitor -n 30" >&2
    exit 1
  fi
  if [ "$NOTIFY_OK" -eq 1 ]; then
    # Значок перезапускаем, чтобы подхватил обновлённый код; второй экземпляр
    # он сам не допустит (блокировка в devmon-tray.py).
    pkill -f "$APP_DIR/deploy/tray/devmon-tray.py" 2>/dev/null || true
    sleep 1
    nohup env DM_TRAY_URL="http://127.0.0.1:$PORT" DM_TRAY_TOKEN_FILE="$DATA/tray-token" \
      /usr/bin/python3 "$APP_DIR/deploy/tray/devmon-tray.py" >/dev/null 2>&1 &
  fi
fi

echo
echo "Готово. Панель: http://127.0.0.1:$PORT  (в меню приложений — «Device Monitor»)"
if [ -f "$DATA/first-login.txt" ]; then
  echo "Логин и пароль первого входа: $DATA/first-login.txt — смените пароль в «Аккаунт»."
fi
if [ "$NOTIFY_OK" -eq 0 ]; then
  echo
  echo "Значок и уведомления Ubuntu не запустятся: нет PyGObject/libnotify."
  echo "  sudo apt install python3-gi gir1.2-notify-0.7 gir1.2-gtk-3.0"
elif [ "$TRAY_OK" -eq 0 ]; then
  echo
  echo "Значка в трее не будет, пока не поставлен пакет (уведомления работают и без него):"
  echo "  sudo apt install gir1.2-ayatanaappindicator3-0.1"
fi
echo
echo "Дальше: подключите свой VPN, затем в панели «⋯» → «Импорт» файла объектов от коллеги"
echo "(экспорт без паролей) или добавьте объекты сами; Telegram — кнопка «Оповещения»."
