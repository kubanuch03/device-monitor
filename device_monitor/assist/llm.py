"""ИИ-помощник: предлагает понятные имена и категории найденным устройствам.

Провайдер - любой OpenAI-совместимый /v1/chat/completions: локальный Qwen на
vLLM (офисная сеть), OpenAI, Gemini через его OpenAI-совместимый адрес. Адрес,
модель и ключ задаются в настройках («⋯» → «ИИ-помощник»), ключ хранится как
пароли устройств и наружу не отдаётся.

Чего модель НЕ делает. Производителя и порты определяет код (discovery/scan.py)
- проверка Qwen 09.11 показала, что модель путает тип и категорию и не узнаёт
Hikvision по «App-webs». Модель только предлагает человеческое имя и
категорию; категория проверяется по списку, ответ вне формата отбрасывается,
и решение о добавлении всё равно за человеком. Паролей и учёток в запрос не
попадает - только то, что видно без входа (адрес, порты, заголовки).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from device_monitor.domain.errors import ValidationError
from device_monitor.security import deobfuscate, obfuscate

SETTINGS_KEY = "assistant"
CATEGORIES = ("Камеры", "Домофоны", "Шлагбаумы", "Терминалы", "Коммутаторы", "Роутеры",
              "Серверы", "Регистраторы", "Прочее")
# Только эти поля кандидата уходят в модель - список закрытый, чтобы новое
# поле скана не утекло наружу само собой.
FIELDS = ("host", "ports", "vendor", "model", "kind", "title", "server", "name")
BATCH = 40
TIMEOUT = 60
MAX_FIELD = 200


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib при редиректе переносит заголовок Authorization на новый хост -
    ключ API ушёл бы туда, куда перенаправил провайдер. Не следуем вовсе."""
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

SYSTEM_PROMPT = (
    "Ты помогаешь заводить сетевое оборудование парковок и КПП в мониторинг. "
    "Тебе дают список устройств, найденных сканом сети объекта (адрес, открытые порты, "
    "производитель и тип, определённые программой, заголовки веб-интерфейса). "
    "Для каждого предложи короткое понятное имя по-русски (до 40 символов) и категорию "
    f"строго из списка: {', '.join(CATEGORIES)}. Тип и производителя, определённые "
    "программой, не меняй - они надёжнее твоей догадки. Если данных мало - оставь "
    "предложенное программой имя. Ответ - ТОЛЬКО JSON: "
    '{"devices": [{"host": "...", "name": "...", "category": "..."}]}'
)


class Assistant:
    def __init__(self, storage):
        self.storage = storage

    def _settings(self) -> dict:
        return {"base_url": "", "model": "", "api_key_enc": "", "enabled": False,
                **self.storage.get_setting(SETTINGS_KEY)}

    def public(self) -> dict:
        s = self._settings()
        return {"base_url": s["base_url"], "model": s["model"], "has_key": bool(s["api_key_enc"]),
                "enabled": bool(s["enabled"]), "configured": self.configured()}

    def configured(self) -> bool:
        s = self._settings()
        return bool(s["enabled"] and s["base_url"] and s["model"])

    def update(self, payload: dict) -> dict:
        s = self._settings()
        if "base_url" in payload:
            url = str(payload["base_url"] or "").strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                raise ValidationError("Адрес API должен начинаться с http:// или https://")
            # Сменился хост - сохранённый ключ к нему не относится и уйти туда
            # не должен: его надо ввести заново.
            if _host(url) != _host(s["base_url"]) and "api_key" not in payload:
                s["api_key_enc"] = ""
            s["base_url"] = url[:300]
        if "model" in payload:
            s["model"] = str(payload["model"] or "").strip()[:120]
        if "api_key" in payload:
            raw = payload["api_key"]
            if raw is False:
                s["api_key_enc"] = ""
            elif raw is True:
                raise ValidationError("Ключ API - строка")
            elif raw:
                s["api_key_enc"] = obfuscate(str(raw).strip()[:500])
        if "enabled" in payload:
            s["enabled"] = bool(payload["enabled"])
        self.storage.set_setting(SETTINGS_KEY, s)
        return self.public()

    # -- запрос к модели ----------------------------------------------------------

    def _chat(self, messages: list[dict]) -> str:
        s = self._settings()
        if not self.configured():
            raise ValidationError("ИИ-помощник не настроен: «⋯» → «ИИ-помощник»")
        body = {"model": s["model"], "messages": messages, "temperature": 0,
                "response_format": {"type": "json_object"},
                # Qwen3 на vLLM: без «размышлений» - ответ в разы быстрее, а для
                # подбора имён они не нужны. Другие провайдеры поле игнорируют.
                "chat_template_kwargs": {"enable_thinking": False}}
        headers = {"Content-Type": "application/json"}
        if s["api_key_enc"]:
            headers["Authorization"] = f"Bearer {deobfuscate(s['api_key_enc'])}"
        try:
            return _post(s["base_url"], body, headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and "response_format" in exc.read().decode("utf-8", "replace"):
                body.pop("response_format")   # провайдер не умеет JSON-режим
                try:
                    return _post(s["base_url"], body, headers)
                except urllib.error.HTTPError as again:
                    raise ValidationError(f"Модель ответила ошибкой HTTP {again.code}") from None
            raise ValidationError(f"Модель ответила ошибкой HTTP {exc.code}") from None

    def test(self) -> dict:
        text = self._chat([{"role": "user", "content": 'Ответь JSON: {"ok": true}'}])
        return {"ok": True, "reply": text[:200]}

    def suggest_names(self, candidates: list[dict]) -> list[dict]:
        """[{host, name, category}] - только для хостов из запроса, категория
        только из списка. Остальное из ответа модели отбрасывается."""
        clean = []
        for c in candidates:
            if not isinstance(c, dict) or not isinstance(c.get("host"), str):
                continue
            item = {}
            for key in FIELDS:
                value = c.get(key)
                if isinstance(value, str) and value:
                    item[key] = value[:MAX_FIELD]
                elif key == "ports" and isinstance(value, list):
                    item[key] = [p for p in value if isinstance(p, int)][:20]
            clean.append(item)
        if not clean:
            raise ValidationError("Нет устройств для подсказки")
        wanted = {c["host"] for c in clean}
        out: dict[str, dict] = {}
        for start in range(0, len(clean), BATCH):
            chunk = clean[start:start + BATCH]
            text = self._chat([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"devices": chunk}, ensure_ascii=False)},
            ])
            for item in _parse_devices(text):
                host = str(item.get("host") or "")
                if host not in wanted:
                    continue
                name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()[:60]
                category = str(item.get("category") or "")
                out[host] = {"host": host,
                             "name": name or None,
                             "category": category if category in CATEGORIES else None}
        return list(out.values())


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "" if url else ""


def _post(base_url: str, body: dict, headers: dict) -> str:
    request = urllib.request.Request(f"{base_url}/chat/completions",
                                     json.dumps(body).encode(), headers)
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            data = json.load(response)
        return data["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValidationError(f"Модель недоступна или ответила не по формату: "
                              f"{type(exc).__name__}") from None


def _parse_devices(text: str) -> list[dict]:
    """JSON из ответа модели: целиком или первый {...} в тексте."""
    match = re.search(r"\{.*\}", text, re.S) if text else None
    for candidate in (text, match.group(0) if match else None):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        devices = data.get("devices") if isinstance(data, dict) else data
        if isinstance(devices, list):
            return [d for d in devices if isinstance(d, dict)]
    return []
