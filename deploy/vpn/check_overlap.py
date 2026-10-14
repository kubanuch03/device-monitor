#!/usr/bin/env python3
"""Проверка: не пересекаются ли AllowedIPs туннелей, которые собираемся поднять.

Пересечение значит, что на хосте один и тот же адрес ведёт сразу в два
туннеля: маршрутизация выберет один, и трафик второго туда не попадёт.
Скрипт ничего не меняет, только читает конфиги и сообщает о конфликтах.

    check_overlap.py <каталог с .conf> <имя> [имя ...]
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path


def allowed_ips(path: Path) -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "AllowedIPs":
            for item in value.split(","):
                item = item.strip()
                if item:
                    networks.append(ipaddress.ip_network(item, strict=False))
    return networks


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    base = Path(argv[1])
    names = argv[2:]
    tunnels = {name: allowed_ips(base / f"{name}.conf") for name in names}

    conflicts = 0
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for net_a in tunnels[a]:
                for net_b in tunnels[b]:
                    if net_a.overlaps(net_b):
                        conflicts += 1
                        print(f"КОНФЛИКТ: {a} {net_a} пересекается с {b} {net_b}")
    if conflicts:
        print(f"Найдено пересечений: {conflicts}. Поднимайте такие туннели только через namespace.")
        return 1
    print("Пересечений AllowedIPs нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
