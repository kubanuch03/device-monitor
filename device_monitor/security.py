"""Пароли: панели и устройств.

Кодирование паролей устройств НЕ выдаётся за шифрование. Настоящее шифрование
требовало бы ключа, который лежал бы в том же окружении рядом с данными и
который сервис обязан уметь применить сам, чтобы ходить на камеры, - защита
от того, у кого есть доступ к хосту, была бы нулевой. Ровно этот случай уже
разобран в SmartParking: EncryptedCharField шифровал пароль камеры в базе и
отдавал его открытым текстом в API, потому что расшифровка шла уровнем выше.

Смысл кодирования здесь один: пароль не читается взглядом при случайном
открытии файла базы. Реальная защита - права 600, пароль на панель и то, что
наружу через API пароль не отдаётся никогда.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

from device_monitor.config import PASSWORD, SESSION_TTL


def password_matches(candidate: str) -> bool:
    # Сравнение по хешу и в постоянное время: наивное `==` на строках
    # завершается на первом несовпавшем символе и по времени ответа выдаёт,
    # сколько символов угадано.
    left = hashlib.sha256(candidate.encode("utf-8")).digest()
    right = hashlib.sha256(PASSWORD.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def obfuscate(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def deobfuscate(value: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


class Sessions:
    """Сессии входа в памяти процесса.

    Перезапуск разлогинивает всех - и это дешевле, чем хранить долгоживущие
    токены на диске: сессия стоит один ввод пароля, а файл с токенами был бы
    лишней поверхностью для утечки.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = time.time() + SESSION_TTL
            self._prune()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            self._prune()
            return token in self._tokens

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, exp in self._tokens.items() if exp < now]:
            self._tokens.pop(token, None)


SESSIONS = Sessions()
