"""FastAPI-приложение: транспорт и версионированный API /api/v1.

Заменяет ручной http.server. Бизнес-логика не переписана — роуты вызывают те же
функции web/api.py и валидацию domain/validation, а Pydantic-схемы дают
типобезопасный вход и автоматический OpenAPI на /docs. Опрос, вендоры,
хранилище и лаунчер профилей не затронуты.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from device_monitor import config
from device_monitor.app import App, migrate_json
from device_monitor.domain.errors import ConflictError, ValidationError
from device_monitor.security import SESSIONS, hash_password, verify_password
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
        f"Device Monitor (FastAPI) готов · устройств: {len(application.storage.devices())} · "
        f"вход: {'по паролю' if config.AUTH_ENABLED else 'открыт'}",
        flush=True,
    )
    try:
        yield
    finally:
        application.stop()


app = FastAPI(title="Device Monitor", version="1.0", lifespan=lifespan)


def core() -> App:
    return APP_STATE["app"]


# -- ошибки бизнес-логики → HTTP -----------------------------------------------

@app.exception_handler(ValidationError)
async def _validation_handler(request: Request, exc: ValidationError):
    # ConflictError — подвид ValidationError; и то и другое это 400 с текстом.
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# -- авторизация ---------------------------------------------------------------

def require_auth(request: Request) -> None:
    if not config.AUTH_ENABLED:
        return
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


@app.post("/api/v1/login")
def login(body: LoginIn, response: Response):
    user = core().storage.get_user()
    # Логин сверяем без учёта регистра, пароль — по хешу. Оба неверных случая
    # (нет такого логина / неверный пароль) отвечают одинаково и с задержкой,
    # чтобы не подсказывать перебору, что именно не так.
    ok = bool(user) and user["username"].casefold() == body.username.strip().casefold() \
        and verify_password(body.password, user["password_hash"])
    if not ok:
        time.sleep(1)
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    _set_cookie(response, SESSIONS.create())
    return {"ok": True}


@app.post("/api/v1/account", dependencies=[Depends(require_auth)])
def change_account(body: AccountIn, request: Request):
    """Смена логина и/или пароля. Требует текущий пароль."""
    user = core().storage.get_user()
    if not user or not verify_password(body.current_password, user["password_hash"]):
        time.sleep(1)
        raise HTTPException(status_code=403, detail="Текущий пароль неверен")
    new_username = (body.username or user["username"]).strip()
    if not new_username:
        raise HTTPException(status_code=400, detail="Логин не может быть пустым")
    new_hash = hash_password(body.new_password) if body.new_password else user["password_hash"]
    core().storage.set_user(new_username, new_hash)
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


@app.get("/api/v1/health")
def health():
    return {"ok": True, "time": api.now_iso()}


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
    return _unwrap(api.create_site(core(), body.model_dump()))


@app.put("/api/v1/sites/{name}", dependencies=[Depends(require_auth)])
def update_site(name: str, body: SiteIn):
    return _unwrap(api.update_site(core(), name, body.model_dump()))


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

app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/")
def index(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE)
    authed = not config.AUTH_ENABLED or SESSIONS.valid(token)
    page = "index.html" if authed else "login.html"
    return FileResponse(config.STATIC_DIR / page)
