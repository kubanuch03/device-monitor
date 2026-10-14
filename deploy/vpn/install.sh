#!/usr/bin/env bash
# Установка WireGuard-туннелей на хост: проверяет конфиги, копирует их в
# /etc/wireguard и включает штатные юниты wg-quick@<имя> (автозапуск при
# загрузке). Запускать от root:
#
#   sudo deploy/vpn/install.sh <каталог с .conf> <имя> [имя ...]
#   sudo deploy/vpn/install.sh ~/vpn wg0 wg1
#
# Конфиги с приватными ключами в репозиторий не кладём: скрипт берёт их из
# каталога, который вы указали. Перед установкой проверяется, что:
#   - ни в одном туннеле нет 0.0.0.0/0 (иначе весь трафик уйдёт в VPN);
#   - AllowedIPs выбранных туннелей не пересекаются (иначе маршрутизация
#     перепутает их; см. check_overlap.py).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужен root: sudo $0 ..." >&2
  exit 1
fi
if [ "$#" -lt 2 ]; then
  echo "Использование: sudo $0 <каталог с .conf> <имя> [имя ...]" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(cd "$1" && pwd)"
shift
TUNNELS=("$@")

for name in "${TUNNELS[@]}"; do
  src="$SRC_DIR/$name.conf"
  if [ ! -f "$src" ]; then
    echo "Нет файла: $src" >&2
    exit 1
  fi
  if grep -qE '^[[:space:]]*AllowedIPs[[:space:]]*=.*0\.0\.0\.0/0' "$src"; then
    echo "$name: AllowedIPs содержит 0.0.0.0/0 - весь трафик уйдёт в этот туннель. Отказ." >&2
    exit 1
  fi
done

if ! python3 "$HERE/check_overlap.py" "$SRC_DIR" "${TUNNELS[@]}"; then
  echo "Установка остановлена: пересечения подсетей. Уберите конфликтующий туннель или используйте namespace." >&2
  exit 1
fi

for name in "${TUNNELS[@]}"; do
  src="$SRC_DIR/$name.conf"
  dst="/etc/wireguard/$name.conf"
  # DNS из конфига оставляем как есть: на этой машине через wg1 идёт весь DNS
  # (systemd-resolved, resolvconf установлен). Удалить строку DNS значит
  # оставить систему без имён, если у основного интерфейса DNS не задан.
  install -m 600 -o root -g root "$src" "$dst"
  systemctl enable --now "wg-quick@$name"
  echo "поднят: wg-quick@$name"
done

echo
wg show
