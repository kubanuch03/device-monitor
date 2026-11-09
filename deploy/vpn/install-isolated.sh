#!/usr/bin/env bash
# Изолированный WireGuard-туннель для объекта, чьи подсети совпадают с уже
# поднятым туннелем (пример: kk и wg0 оба заявляют 172.30.11.0/24 и
# 192.168.158.0/24, но за ними РАЗНЫЕ сети).
#
#   sudo deploy/vpn/install-isolated.sh <каталог с .conf> <имя> <таблица> <порт SOCKS>
#   sudo deploy/vpn/install-isolated.sh ~/vpn kk 1050 1082
#
# Как устроено:
#   - туннель пишет маршруты не в main, а в свою таблицу (Table = <таблица>),
#     поэтому маршруты wg0 не трогаются вообще;
#   - в эту таблицу смотрит только системный пользователь dm-<имя> (ip rule
#     uidrange), от которого работает microsocks на 127.0.0.1:<порт>;
#   - в панели у точки объекта прокси socks5://127.0.0.1:<порт> - дальше
#     работает тот же механизм, что у точек за ssh -D.
#
# Правило unreachable после таблицы обязательно: без него при упавшем туннеле
# трафик прокси провалился бы в main, то есть в wg0, и монитор показывал бы
# живыми устройства ЧУЖОГО объекта с теми же адресами.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужен root: sudo $0 ..." >&2
  exit 1
fi
if [ "$#" -ne 4 ]; then
  echo "Использование: sudo $0 <каталог с .conf> <имя> <таблица> <порт SOCKS>" >&2
  exit 2
fi

SRC="$(cd "$1" && pwd)/$2.conf"
NAME="$2"
TABLE="$3"
PORT="$4"
USER_NAME="dm-$NAME"
UNIT="device-socks-$NAME"

[ -f "$SRC" ] || { echo "Нет файла: $SRC" >&2; exit 1; }
if grep -qE '^[[:space:]]*AllowedIPs[[:space:]]*=.*0\.0\.0\.0/0' "$SRC"; then
  echo "$NAME: AllowedIPs содержит 0.0.0.0/0. Отказ." >&2
  exit 1
fi
if grep -qiE '^[[:space:]]*(Table|PostUp|PreDown|PostDown)[[:space:]]*=' "$SRC"; then
  echo "$NAME: в конфиге уже есть Table/PostUp/PreDown - скрипт их задаёт сам, уберите." >&2
  exit 1
fi
if [ -n "$(ip route show table "$TABLE" 2>/dev/null)" ] && ! ip link show "$NAME" >/dev/null 2>&1; then
  echo "Таблица $TABLE уже занята чем-то другим: $(ip route show table "$TABLE" | head -1)" >&2
  exit 1
fi

command -v microsocks >/dev/null || apt-get install -y microsocks

id "$USER_NAME" >/dev/null 2>&1 \
  || useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
UID_N="$(id -u "$USER_NAME")"

# Конфиг туннеля: оригинал + Table сразу после [Interface].
awk -v t="$TABLE" '{print} /^\[Interface\]/{print "Table = " t}' "$SRC" > "/etc/wireguard/$NAME.conf"
chmod 600 "/etc/wireguard/$NAME.conf"

# Правила живут вместе с прокси, а не с туннелем: пока прокси запущен, он
# обязан смотреть только в свою таблицу, поднят туннель или нет.
cat > "/etc/systemd/system/$UNIT.service" <<EOF
[Unit]
Description=Device Monitor: SOCKS-прокси в изолированный туннель $NAME
After=wg-quick@$NAME.service
Wants=wg-quick@$NAME.service

[Service]
User=$USER_NAME
ExecStartPre=+-/usr/sbin/ip rule del uidrange $UID_N-$UID_N lookup $TABLE priority $TABLE
ExecStartPre=+-/usr/sbin/ip rule del uidrange $UID_N-$UID_N unreachable priority $((TABLE + 1))
ExecStartPre=+/usr/sbin/ip rule add uidrange $UID_N-$UID_N lookup $TABLE priority $TABLE
ExecStartPre=+/usr/sbin/ip rule add uidrange $UID_N-$UID_N unreachable priority $((TABLE + 1))
ExecStart=/usr/bin/microsocks -i 127.0.0.1 -p $PORT
ExecStopPost=+-/usr/sbin/ip rule del uidrange $UID_N-$UID_N lookup $TABLE priority $TABLE
ExecStopPost=+-/usr/sbin/ip rule del uidrange $UID_N-$UID_N unreachable priority $((TABLE + 1))
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "wg-quick@$NAME"
systemctl enable --now "$UNIT"
sleep 2

echo
wg show "$NAME"
echo
FIRST="$(grep -E '^[[:space:]]*AllowedIPs' "$SRC" | head -1 | cut -d= -f2 | cut -d, -f1 | cut -d/ -f1 | tr -d ' ')"
echo "Маршрут прокси до $FIRST: $(ip route get "$FIRST" uid "$UID_N" 2>&1 | head -1)"
echo "Маршрут остальных до $FIRST: $(ip route get "$FIRST" 2>&1 | head -1)"
echo
echo "Готово. В панели у точки объекта: socks5://127.0.0.1:$PORT"
