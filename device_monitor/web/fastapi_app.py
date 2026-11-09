"""FastAPI-приложение: транспорт и версионированный API /api/v1.

Заменяет ручной http.server. Бизнес-логика не переписана — роуты вызывают те же
функции web/api.py и валидацию domain/validation, а Pydantic-схемы дают
типобезопасный вход и автоматический OpenAPI на /docs. Опрос, вендоры,
хранилище и лаунчер профилей не затронуты.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from device_monitor import config
from device_monitor.app import App, migrate_json
from device_monitor.domain.errors import ConflictError, ValidationError
from device_monitor.security import SESSIONS, THROTTLE, hash_password, verify_password
from device_monitor.web import api
from device_monitor.web.schemas import AccountIn, CategoryIn, DeviceIn, LoginIn, SiteIn

APP_STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    application = App()
    migrate_json(application.storage, config.LEGACY_JSON)
    application.start()
    APP_STATE["app"] = application
    print(
        f"Device Monitor (FastAPI) готов · устройств: {len(application.storage.devices())}",
        flush=True,
    )
    try:
        yield
    finally:
        application.stop()


# /docs и /openapi.json выключены: без входа они раскрывали бы всю карту API
# любому, кто дошёл до порта.
app = FastAPI(title="Device Monitor", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _no_store_api(request: Request, call_next):
    """Ответы API не кэшируются нигде: в них пароли (/secret, экспорт) и
    живое состояние, которое из кэша было бы враньём."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def core() -> App:
    return APP_STATE["app"]


# -- ошибки бизнес-логики → HTTP -----------------------------------------------

@app.exception_handler(ValidationError)
async def _validation_handler(request: Request, exc: ValidationError):
    # ConflictError — подвид ValidationError; и то и другое это 400 с текстом.
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# -- авторизация ---------------------------------------------------------------

def require_auth(request: Request) -> None:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not SESSIONS.valid(token):
        raise HTTPException(status_code=401, detail="Нужен вход")


def _set_cookie(response: Response, token: str | None) -> None:
    # Secure не ставим: по умолчанию 127.0.0.1 по http, иначе cookie не
    # сохранится. Наружу — только за HTTPS-прокси (см. README).
    if token:
        response.set_cookie(
            config.SESSION_COOKIE, token, max_age=config.SESSION_TTL,
            httponly=True, samesite="strict", path="/",
        )
    else:
        response.delete_cookie(config.SESSION_COOKIE, path="/")


# -- аутентификация ------------------------------------------------------------

@app.get("/api/v1/session")
def session(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE)
    authed = SESSIONS.valid(token)
    user = core().storage.get_user() if authed else None
    return {
        "auth_required": True,
        "authenticated": authed,
        "username": user["username"] if user else None,
    }


def _client_key(request: Request, scope: str) -> str:
    return f"{scope}:{request.client.host if request.client else '-'}"


def _check_throttle(key: str) -> None:
    wait = THROTTLE.acquire(key)
    if wait:
        raise HTTPException(status_code=429, detail=f"Слишком много неудачных попыток. "
                                                    f"Повторите через {wait} с",
                            headers={"Retry-After": str(wait)})


@app.post("/api/v1/login")
def login(body: LoginIn, request: Request, response: Response):
    key = _client_key(request, "login")
    _check_throttle(key)
    user = core().storage.get_user()
    # Логин сверяем без учёта регистра, пароль — по хешу. Оба неверных случая
    # (нет такого логина / неверный пароль) отвечают одинаково, чтобы не
    # подсказывать перебору, что именно не так.
    ok = bool(user) and user["username"].casefold() == body.username.strip().casefold() \
        and verify_password(body.password, user["password_hash"])
    if not ok:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    THROTTLE.reset(key)
    _set_cookie(response, SESSIONS.create())
    return {"ok": True}


@app.post("/api/v1/account", dependencies=[Depends(require_auth)])
def change_account(body: AccountIn, request: Request):
    """Смена логина и/или пароля. Требует текущий пароль."""
    key = _client_key(request, "account")
    _check_throttle(key)
    user = core().storage.get_user()
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=403, detail="Текущий пароль неверен")
    THROTTLE.reset(key)
    new_username = (body.username or user["username"]).strip()
    if not new_username:
        raise HTTPException(status_code=400, detail="Логин не может быть пустым")
    new_hash = hash_password(body.new_password) if body.new_password else user["password_hash"]
    core().storage.set_user(new_username, new_hash)
    if body.new_password:
        SESSIONS.drop_others(request.cookies.get(config.SESSION_COOKIE))
        # Сгенерированный пароль первого входа больше не действует.
        config.FIRST_LOGIN_FILE.unlink(missing_ok=True)
    return {"ok": True, "username": new_username}


@app.post("/api/v1/logout")
def logout(request: Request, response: Response):
    SESSIONS.drop(request.cookies.get(config.SESSION_COOKIE))
    _set_cookie(response, None)
    return {"ok": True}


# -- вспомогательное: (status, body) из api.* → тело или HTTPException ----------

def _unwrap(result):
    status, body = result
    if status >= 400:
        raise HTTPException(status_code=status, detail=body.get("detail", "Ошибка"))
    return body


# -- состояние -----------------------------------------------------------------

@app.get("/api/v1/state", dependencies=[Depends(require_auth)])
def state():
    return _unwrap(api.state(core()))


@app.get("/api/v1/sites/{name}/streams", dependencies=[Depends(require_auth)])
def site_streams(name: str):
    return _unwrap(api.stream_names(core(), name))


@app.get("/api/v1/health")
def health():
    return {"ok": True, "time": api.now_iso()}


@app.get("/api/v1/export", dependencies=[Depends(require_auth)])
def export_data(secrets: bool = False):
    # Отдаём файлом: браузер сохранит его как есть, имя с датой.
    body = _unwrap(api.export_data(core(), secrets))
    stamp = body["exported_at"][:10]
    return JSONResponse(
        content=body,
        headers={"Content-Disposition": f'attachment; filename="device-monitor-{stamp}.json"'},
    )


@app.post("/api/v1/import", dependencies=[Depends(require_auth)])
def import_data(payload: dict = Body(...)):
    return _unwrap(api.import_data(core(), payload))


@app.post("/api/v1/recheck", dependencies=[Depends(require_auth)])
def recheck():
    return _unwrap(api.recheck(core()))


# -- устройства ----------------------------------------------------------------

@app.post("/api/v1/devices", dependencies=[Depends(require_auth)])
def create_device(body: DeviceIn):
    return _unwrap(api.create_device(core(), body.model_dump(exclude_none=True)))


@app.put("/api/v1/devices/{device_id}", dependencies=[Depends(require_auth)])
def update_device(device_id: str, body: DeviceIn):
    return _unwrap(api.update_device(core(), device_id, body.model_dump(exclude_none=True)))


@app.delete("/api/v1/devices/{device_id}", dependencies=[Depends(require_auth)])
def delete_device(device_id: str):
    return _unwrap(api.delete_device(core(), device_id))


@app.get("/api/v1/devices/{device_id}/history", dependencies=[Depends(require_auth)])
def device_history(device_id: str):
    return _unwrap(api.device_history(core(), device_id))


@app.get("/api/v1/devices/{device_id}/secret", dependencies=[Depends(require_auth)])
def device_secret(device_id: str):
    return _unwrap(api.device_secret(core(), device_id))


@app.post("/api/v1/devices/{device_id}/inspect", dependencies=[Depends(require_auth)])
def inspect(device_id: str):
    return _unwrap(api.inspect(core(), device_id))


# -- точки ---------------------------------------------------------------------

@app.post("/api/v1/sites", dependencies=[Depends(require_auth)])
def create_site(body: SiteIn):
    return _unwrap(api.create_site(core(), body.model_dump(exclude_none=True)))


@app.put("/api/v1/sites/{name}", dependencies=[Depends(require_auth)])
def update_site(name: str, body: SiteIn):
    return _unwrap(api.update_site(core(), name, body.model_dump(exclude_none=True)))


@app.delete("/api/v1/sites/{name}", dependencies=[Depends(require_auth)])
def delete_site(name: str):
    return _unwrap(api.delete_site(core(), name))


# -- категории -----------------------------------------------------------------

@app.post("/api/v1/categories", dependencies=[Depends(require_auth)])
def create_category(body: CategoryIn):
    return _unwrap(api.create_category(core(), body.model_dump()))


@app.put("/api/v1/categories/{site}/{name}", dependencies=[Depends(require_auth)])
def update_category(site: str, name: str, body: CategoryIn):
    return _unwrap(api.update_category(core(), site, name, body.model_dump()))


@app.delete("/api/v1/categories/{site}/{name}", dependencies=[Depends(require_auth)])
def delete_category(site: str, name: str):
    return _unwrap(api.delete_category(core(), site, name))


# -- статика и страницы --------------------------------------------------------

class RevalidatedStatic(StaticFiles):
    """Статика с обязательной перепроверкой.

    Без Cache-Control браузер кэширует файлы «на глаз» и после обновления
    сервиса продолжает гонять старый app.js: новые кнопки и пути API не
    появляются. no-cache не отключает кэш, а заставляет спросить сервер -
    по ETag ответ 304 без тела, так что это почти бесплатно.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStatic(directory=str(config.STATIC_DIR)), name="static")


@app.get("/")
def index(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE)
    authed = SESSIONS.valid(token)
    page = "index.html" if authed else "login.html"
    # no-cache обязателен: по одному адресу отдаются две разные страницы, и
    # закэшированная форма входа показывалась бы уже вошедшему.
    return FileResponse(config.STATIC_DIR / page, headers={"Cache-Control": "no-cache"})
