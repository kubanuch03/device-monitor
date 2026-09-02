#!/usr/bin/env bash
# Регистрирует обработчик ссылок devmon:// на лаунчер device-open, чтобы кнопка
# «Открыть» в дашборде поднимала браузер-профиль точки. Разовая установка,
# от обычного пользователя (не root) — handler ставится в его ~/.local.
set -euo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
sed "s#/PATH/TO/device-monitor/deploy/device-open#$HERE/device-open#" \
    "$HERE/device-open.desktop" > "$APPS/device-open.desktop"
update-desktop-database "$APPS" 2>/dev/null || true

# Прописываем default в обоих mimeapps.list: ~/.config имеет приоритет над
# ~/.local/share, а xdg-mime default не всегда пишет в нужный.
for MA in "$HOME/.config/mimeapps.list" "$APPS/mimeapps.list"; do
    mkdir -p "$(dirname "$MA")"; touch "$MA"
    grep -q "^\[Default Applications\]" "$MA" || echo "[Default Applications]" >> "$MA"
    grep -q "x-scheme-handler/devmon=" "$MA" || \
        sed -i "/^\[Default Applications\]/a x-scheme-handler/devmon=device-open.desktop" "$MA"
done
xdg-mime default device-open.desktop x-scheme-handler/devmon 2>/dev/null || true
echo "✓ Обработчик devmon:// зарегистрирован на $HERE/device-open"
echo "  Проверка: xdg-open 'devmon://open?url=http://example.com/'"
