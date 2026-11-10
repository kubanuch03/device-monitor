#!/usr/bin/env bash
# Сборка device-monitor_<версия>_amd64.deb: программа (Rust/Tauri) + сервис
# мониторинга (упакован PyInstaller'ом) + автозапуск. Собирается на машине
# разработчика; сотруднику нужен только готовый .deb.
#
# Один раз на машине сборки:
#   sudo apt install build-essential pkg-config libssl-dev libwebkit2gtk-4.1-dev \
#        libayatana-appindicator3-dev librsvg2-dev
#   (Rust - rustup, Node - nvm; venv проекта с requirements.txt)
#
#   deploy/desktop/build-deb.sh      → deploy/desktop/dist/device-monitor_1.0.0_amd64.deb
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$ROOT/deploy/desktop/app"
cd "$ROOT"

# 1. Сервис - в самостоятельный исполняемый файл (Python пользователю не нужен).
.venv/bin/pip install --quiet pyinstaller==6.22.3
rm -rf build/server
.venv/bin/pyinstaller --noconfirm --clean --onedir --name device-monitor-server \
  --distpath build/server/dist --workpath build/server/work --specpath build/server \
  --paths "$ROOT" --add-data "$ROOT/static:static" \
  --collect-submodules uvicorn --hidden-import sqlalchemy.dialects.sqlite \
  --log-level WARN monitor.py

# 2. Программа и .deb.
NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)"
export PATH="${NODE_BIN:+$NODE_BIN:}$HOME/.cargo/bin:$PATH"
cd "$APP"
npm ci --no-audit --no-fund --silent
npx tauri build --bundles deb

VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' src-tauri/tauri.conf.json)"
mkdir -p "$ROOT/deploy/desktop/dist"
OUT="$ROOT/deploy/desktop/dist/device-monitor_${VERSION}_amd64.deb"
cp "src-tauri/target/release/bundle/deb/Device Monitor_${VERSION}_amd64.deb" "$OUT"
echo
echo "Готово: $OUT"
echo "Установка: sudo apt install ./$(basename "$OUT")   (или двойной клик в «Центре приложений»)"
