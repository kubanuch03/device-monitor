#!/usr/bin/env bash
# Заводит постоянный SOCKS-туннель до одного объекта для Device Monitor —
# одной командой, вместо ручного копирования файлов.
#
#   sudo ./deploy/setup-tunnel.sh <имя-объекта> <ssh-target> <socks-порт>
#   пример: sudo ./deploy/setup-tunnel.sh balykchy balykchy 1081
#
# <ssh-target> — как в `ssh <target>`: алиас из ~/.ssh/config или user@host.
# Туннель поднимается С ЭТОЙ машины; на сервере объекта ставить ничего не надо.
#
# Почему это скрипт на хосте, а не функция device-monitor: туннель ходит по
# приватному SSH-ключу, а держать ключ внутри контейнера сервиса нельзя —
# это ровно тот запечённый-в-образ секрет, которого мы избегаем.
set -euo pipefail

NAME="${1:-}"; TARGET="${2:-}"; PORT="${3:-}"
if [ -z "$NAME" ] || [ -z "$TARGET" ] || [ -z "$PORT" ]; then
    echo "Использование: sudo $0 <имя-объекта> <ssh-target> <socks-порт>" >&2
    echo "Пример:        sudo $0 balykchy balykchy 1081" >&2
    exit 2
fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "Порт должен быть числом 1–65535, а не «$PORT»" >&2; exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "Нужны права root (sudo): скрипт пишет в /etc и дёргает systemctl." >&2; exit 2
fi

# От чьего имени пойдёт SSH: того, кто вызвал sudo, — у него ключ и ~/.ssh/config.
RUN_USER="${SUDO_USER:-$USER}"
UNIT_SRC="$(dirname "$(readlink -f "$0")")/device-tunnel@.service"
CONF_DIR="/etc/device-monitor/tunnels"

command -v autossh >/dev/null || { echo "Нет autossh. Установите: apt install autossh" >&2; exit 1; }
[ -f "$UNIT_SRC" ] || { echo "Не найден $UNIT_SRC" >&2; exit 1; }

echo "→ проверяю SSH-доступ к «$TARGET» от пользователя $RUN_USER…"
if ! sudo -u "$RUN_USER" ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" true 2>/dev/null; then
    echo "  Не удалось зайти по SSH на «$TARGET» без пароля." >&2
    echo "  Настройте ключ (ssh-copy-id $TARGET) и повторите." >&2
    exit 1
fi
echo "  SSH работает."

# Юнит с правильным пользователем внутри.
install -m 644 <(sed "s/^User=.*/User=$RUN_USER/" "$UNIT_SRC") /etc/systemd/system/device-tunnel@.service
mkdir -p "$CONF_DIR"
cat > "$CONF_DIR/$NAME.conf" <<CONF
SSH_TARGET=$TARGET
SOCKS_PORT=$PORT
CONF
chmod 600 "$CONF_DIR/$NAME.conf"

systemctl daemon-reload
systemctl enable --now "device-tunnel@$NAME"

echo "→ жду, пока туннель забиндит порт $PORT…"
for _ in $(seq 1 20); do
    if ss -tln 2>/dev/null | grep -q "127.0.0.1:$PORT "; then
        echo "✓ Туннель device-tunnel@$NAME поднят, SOCKS на 127.0.0.1:$PORT"
        echo
        echo "  Теперь в панели у точки этого объекта впишите в поле прокси:"
        echo "     socks5://127.0.0.1:$PORT"
        echo "  Логи туннеля:  journalctl -u device-tunnel@$NAME -f"
        exit 0
    fi
    sleep 0.5
done
echo "✗ Порт $PORT не забиндился за 10с. Смотрите: journalctl -u device-tunnel@$NAME" >&2
exit 1
