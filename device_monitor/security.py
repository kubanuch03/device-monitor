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

from device_monitor.config import LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW, SESSION_TTL

# Пароль ВХОДА в панель хранится необратимым хешем (в отличие от паролей
# устройств, которые обратимы — они нужны для digest-логина на камеру).
# pbkdf2-hmac-sha256 из стандартной библиотеки, со случайной солью.
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        # Сравнение в постоянное время — не выдаёт по таймингу, сколько угадано.
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


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

    def drop_others(self, keep: str | None) -> None:
        """После смены пароля: все остальные входы недействительны.

        Иначе тот, кто знал старый пароль и успел войти, остался бы в панели
        до истечения сессии - смена пароля его бы не выкинула.
        """
        with self._lock:
            expiry = self._tokens.get(keep) if keep else None
            self._tokens.clear()
            if expiry is not None:
                self._tokens[keep] = expiry

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, exp in self._tokens.items() if exp < now]:
            self._tokens.pop(token, None)


class LoginThrottle:
    """Ограничение перебора пароля: не больше limit попыток с адреса за окно.

    Заменяет прежний time.sleep(1) в обработчике: тот не мешал перебору
    (запросы шли параллельно), зато занимал поток из пула на секунду на
    каждую попытку - то есть сам был рычагом для DoS.

    Попытка резервируется ДО проверки пароля, одной операцией под блокировкой.
    Раздельные «проверить» и «записать неудачу» пропускали пачку: между ними
    идёт pbkdf2 на 200 тыс. раундов, и все параллельные запросы успевали
    пройти проверку, пока ни один не записал неудачу. Успешный вход снимает
    счётчик целиком.
    """

    MAX_KEYS = 10_000

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def _prune_all(self, now: float) -> None:
        for key in [k for k, v in self._fails.items() if now - v[-1] >= self.window]:
            del self._fails[key]

    def acquire(self, key: str) -> int:
        """Резервирует попытку. 0 - можно проверять пароль; иначе сколько
        секунд ждать (попытка тогда не засчитывается)."""
        now = time.time()
        with self._lock:
            # Без общей чистки ключи адресов, которые больше не приходят,
            # копились бы бесконечно.
            if len(self._fails) >= self.MAX_KEYS:
                self._prune_all(now)
            stamps = [t for t in self._fails.get(key, []) if now - t < self.window]
            if len(stamps) >= self.limit:
                self._fails[key] = stamps
                return max(1, int(self.window - (now - stamps[-self.limit])) + 1)
            stamps.append(now)
            self._fails[key] = stamps
            return 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


SESSIONS = Sessions()
THROTTLE = LoginThrottle(LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW)
