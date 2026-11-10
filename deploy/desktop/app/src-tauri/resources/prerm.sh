#!/bin/sh
# Только при удалении: при обновлении (upgrade) сервис остаётся включённым.
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  for dir in /run/user/*; do
    uid="${dir##*/}"
    systemctl --machine="$uid@" --user stop device-monitor.service >/dev/null 2>&1 || true
  done
  systemctl --global disable device-monitor.service >/dev/null 2>&1 || true
  pkill -x device-monitor >/dev/null 2>&1 || true
fi
exit 0
