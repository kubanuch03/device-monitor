"""Транспорт: stdlib-HTTP поверх функций из web/api.py.

Единственный модуль, который знает про заголовки, cookie и статус-коды. Вся
логика живёт в api.py и ничего про HTTP не знает - поэтому замена этого файла
на FastAPI была бы заменой транспорта, а не переписыванием приложения.
"""

from __future__ import annotations

import json
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from device_monitor import config
from device_monitor.domain.errors import ValidationError
from device_monitor.security import SESSIONS, password_matches
from device_monitor.web import api

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DeviceMonitor"
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, fmt, *args):
        return

    # -- ответы ---------------------------------------------------------------

    def _send(self, status, body: bytes, content_type: str, cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, data, cookie: str | None = None) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", cookie)

    def _error(self, status, detail: str) -> None:
        self._json(status, {"detail": detail})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > config.MAX_BODY_BYTES:
            raise ValidationError("Слишком большой запрос")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ValidationError("Некорректный JSON")

    def _static(self, rel: str) -> None:
        target = (config.STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(config.STATIC_DIR.resolve())) or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Не найдено")
            return
        self._send(HTTPStatus.OK, target.read_bytes(),
                   CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    # -- авторизация ------------------------------------------------------------

    def _cookie(self, name: str) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _authed(self) -> bool:
        return not config.AUTH_ENABLED or SESSIONS.valid(self._cookie(config.SESSION_COOKIE))

    @staticmethod
    def _session_cookie(token: str | None) -> str:
        # Secure не ставим: сервис по умолчанию слушает 127.0.0.1 по http, и с
        # этим флагом cookie просто не сохранится. Прямое следствие записано в
        # README: наружу без TLS панель открывать нельзя - пароль и cookie
        # пойдут по сети открытым текстом.
        base = f"{config.SESSION_COOKIE}={token or ''}; Path=/; HttpOnly; SameSite=Strict"
        return f"{base}; Max-Age={config.SESSION_TTL}" if token else f"{base}; Max-Age=0"

    # -- разбор ошибок ------------------------------------------------------------

    def _dispatch(self, handler) -> None:
        """Ни одно исключение не должно доходить до сокета.

        Без этого необработанная ошибка обрывала соединение вообще без ответа,
        и клиент видел «Empty reply from server» - сообщение, по которому
        невозможно понять причину. Так и случилось с нехваткой прав на запись:
        устройство добавлялось в память, запись падала, ответа не было вовсе.
        """
        try:
            handler()
        except ValidationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except PermissionError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR,
                        f"Нет прав на запись данных ({exc.filename}). "
                        "В докере это обычно несовпадение владельца каталога data и "
                        "пользователя контейнера — см. параметр user в docker-compose.yml.")
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Ошибка файловой системы: {exc}")
        except Exception as exc:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Внутренняя ошибка: {exc}")

    def _reply(self, result) -> None:
        status, data = result
        self._json(status, data)

    # -- маршруты --------------------------------------------------------------

    def do_GET(self):
        self._dispatch(self._get)

    do_HEAD = do_GET

    def do_POST(self):
        self._dispatch(self._post)

    def do_PUT(self):
        self._dispatch(self._put)

    def do_DELETE(self):
        self._dispatch(self._delete)

    def _get(self):
        path = urlparse(self.path).path
        if path == "/":
            self._static("index.html" if self._authed() else "login.html")
        elif path == "/api/session":
            self._json(HTTPStatus.OK,
                       {"auth_required": config.AUTH_ENABLED, "authenticated": self._authed()})
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        elif path == "/api/health":
            # Намеренно ДО проверки входа: это liveness-проба для HEALTHCHECK в
            # Dockerfile, которая ходит без cookie. За авторизацией она отдавала
            # бы 401, и контейнер с включённым DM_PASSWORD становился unhealthy.
            # Секретов в ответе нет - только «процесс жив» и время.
            self._json(HTTPStatus.OK, {"ok": True, "time": api.now_iso()})
        elif not self._authed():
            self._error(HTTPStatus.UNAUTHORIZED, "Нужен вход")
        elif path == "/api/state":
            self._reply(api.state(self.app))
        elif path.startswith("/api/devices/") and path.endswith("/history"):
            self._reply(api.device_history(self.app, path[len("/api/devices/"):-len("/history")]))
        elif path.startswith("/api/devices/") and path.endswith("/secret"):
            self._reply(api.device_secret(self.app, path[len("/api/devices/"):-len("/secret")]))
        else:
            self._error(HTTPStatus.NOT_FOUND, "Не найдено")

    def _post(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            if not config.AUTH_ENABLED:
                self._error(HTTPStatus.BAD_REQUEST, "Вход не настроен")
                return
            if not password_matches(str(self._body().get("password") or "")):
                # Пауза против перебора: без неё пароль панели подбирается со
                # скоростью сети.
                time.sleep(1)
                self._error(HTTPStatus.UNAUTHORIZED, "Неверный пароль")
                return
            self._json(HTTPStatus.OK, {"ok": True}, self._session_cookie(SESSIONS.create()))
            return
        if path == "/api/logout":
            SESSIONS.drop(self._cookie(config.SESSION_COOKIE))
            self._json(HTTPStatus.OK, {"ok": True}, self._session_cookie(None))
            return
        if not self._authed():
            self._error(HTTPStatus.UNAUTHORIZED, "Нужен вход")
            return

        if path == "/api/devices":
            self._reply(api.create_device(self.app, self._body()))
        elif path == "/api/sites":
            self._reply(api.create_site(self.app, self._body()))
        elif path == "/api/categories":
            self._reply(api.create_category(self.app, self._body()))
        elif path == "/api/recheck":
            self._reply(api.recheck(self.app))
        elif path.startswith("/api/devices/") and path.endswith("/inspect"):
            self._reply(api.inspect(self.app, path[len("/api/devices/"):-len("/inspect")]))
        else:
            self._error(HTTPStatus.NOT_FOUND, "Не найдено")

    def _put(self):
        if not self._authed():
            self._error(HTTPStatus.UNAUTHORIZED, "Нужен вход")
            return
        path = urlparse(self.path).path
        if path.startswith("/api/devices/"):
            self._reply(api.update_device(self.app, path[len("/api/devices/"):], self._body()))
        elif path.startswith("/api/sites/"):
            self._reply(api.update_site(self.app, unquote(path[len("/api/sites/"):]), self._body()))
        elif path.startswith("/api/categories/"):
            site, name = self._category_ref(path)
            self._reply(api.update_category(self.app, site, name, self._body()))
        else:
            self._error(HTTPStatus.NOT_FOUND, "Не найдено")

    def _delete(self):
        if not self._authed():
            self._error(HTTPStatus.UNAUTHORIZED, "Нужен вход")
            return
        path = urlparse(self.path).path
        if path.startswith("/api/devices/"):
            self._reply(api.delete_device(self.app, path[len("/api/devices/"):]))
        elif path.startswith("/api/sites/"):
            self._reply(api.delete_site(self.app, unquote(path[len("/api/sites/"):])))
        elif path.startswith("/api/categories/"):
            site, name = self._category_ref(path)
            self._reply(api.delete_category(self.app, site, name))
        else:
            self._error(HTTPStatus.NOT_FOUND, "Не найдено")

    @staticmethod
    def _category_ref(path: str) -> tuple[str, str]:
        """Разбирает /api/categories/{точка}/{категория}.

        Режем сырой путь и только потом декодируем: в названии может оказаться
        слэш, он приезжает как %2F, и при раннем декодировании ссылка
        развалилась бы на лишний сегмент.
        """
        parts = path[len("/api/categories/"):].split("/")
        if len(parts) != 2 or not all(parts):
            raise ValidationError("Некорректная ссылка на категорию")
        return unquote(parts[0]), unquote(parts[1])


def serve(app) -> None:
    Handler.app = app
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(
        f"Device Monitor слушает http://{config.HOST}:{config.PORT} · "
        f"устройств: {len(app.storage.devices())} · опрос каждые {config.POLL_INTERVAL}с · "
        f"вход: {'по паролю' if config.AUTH_ENABLED else 'открыт'}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.server_close()
