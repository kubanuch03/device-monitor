"""Мелкие текстовые утилиты, общие для доменного слоя."""

from __future__ import annotations


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def same_name(left: str, right: str) -> bool:
    """Сравнение имён без учёта регистра — обязательно в Python, не в SQL.

    COLLATE NOCASE в SQLite сворачивает регистр только для латиницы A-Z:
    «Ала-Арча» и «АЛА-АРЧА» проходили как разные имена, и дубль точки
    создавался молча. casefold() работает со всем Unicode.
    """
    return left.casefold() == right.casefold()
