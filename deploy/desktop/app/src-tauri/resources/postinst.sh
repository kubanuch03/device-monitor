#!/bin/sh
# Сервис мониторинга - для каждого пользователя при входе в систему.
systemctl --global enable device-monitor.service >/dev/null 2>&1 || true
# Обновление: у вошедших пользователей перезапускаем уже работающий сервис,
# иначе до перелогина крутился бы старый код. Значок в трее перезапустится
# при следующем открытии программы.
if [ "$1" = "configure" ] && [ -n "$2" ]; then
  for dir in /run/user/*; do
    uid="${dir##*/}"
    systemctl --machine="$uid@" --user try-restart device-monitor.service >/dev/null 2>&1 || true
  done
fi
exit 0
