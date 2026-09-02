# Развёртывание Device Monitor и туннелей до объектов

## Топология

Device Monitor и все туннели крутятся на **одной** машине — там, где вы смотрите
дашборд. На серверах объектов **ничего ставить не нужно**, туда только идёт SSH.

```
ваша машина                                  сервер объекта      устройства
┌────────────────────────────┐              (напр. Балыкчи)     объекта
│ device-monitor (docker)     │                                 ┌─────────┐
│      │ проверка через        │  ssh -D 1081   ┌──────────┐    │ камеры  │
│      ▼                       │ ──────────────►│ SSH-доступ│───►│ роутер  │
│ autossh → SOCKS :1081 ───────┼────────────────│  сервера  │    │ и т.д.  │
│ (systemd, постоянный)        │                └──────────┘    └─────────┘
└────────────────────────────┘
```

Объекты с прямым маршрутом (VPN/локальная сеть) туннеля не требуют — у их точки
поле прокси оставляется пустым, проверка идёт напрямую.

## 1. Сам сервис

```bash
git clone <этот репозиторий> device-monitor && cd device-monitor
cp data/devices.example.json data/devices.json   # или пустой старт
echo "DM_PASSWORD=<сгенерируйте>" > .env
docker compose up -d
```

Панель — http://127.0.0.1:8890.

## 2. Туннель до объекта без прямого маршрута

Нужен только для объектов, до которых у этой машины нет сетевого пути.

```bash
sudo apt install autossh

# файл настроек объекта
sudo mkdir -p /etc/device-monitor/tunnels
sudo cp deploy/tunnels.example/balykchy.conf /etc/device-monitor/tunnels/
# отредактируйте SSH_TARGET и SOCKS_PORT под объект

# сам юнит
sudo cp deploy/device-tunnel@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now device-tunnel@balykchy

systemctl status device-tunnel@balykchy      # проверить, что поднялся
```

Затем в панели у точки этого объекта в поле «SOCKS-прокси точки» указать
`socks5://127.0.0.1:<SOCKS_PORT>` — и её устройства начнут проверяться через
туннель.

## Новый объект = повтор шага 2

Скопировать `<имя>.conf` с другим `SSH_TARGET`/`SOCKS_PORT` (порт уникальный на
каждый объект), `systemctl enable --now device-tunnel@<имя>`, прописать прокси
точке. Один шаблон юнита обслуживает сколько угодно объектов.

## Если туннель ляжет

Панель покажет у точки «нет связи с туннелем объекта» — это отличается от
«устройства не отвечают». autossh переподнимет соединение сам; если не выходит,
смотрите `journalctl -u device-tunnel@<имя>`.
