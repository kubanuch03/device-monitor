"""Telegram Bot API через urllib - без зависимостей.

Запросы идут напрямую в интернет, не через прокси точек: прокси ведут в сети
объектов, а не наружу.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class TelegramError(Exception):
    pass


def _call(token: str, method: str, params: dict | None = None, timeout: float = 10) -> object:
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            raise TelegramError(f"HTTP {exc.code}") from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # Текст исключения urllib может содержать URL с токеном - не пробрасываем.
        raise TelegramError(f"нет связи с Telegram: {type(exc).__name__}") from None
    if not isinstance(body, dict) or not body.get("ok"):
        description = body.get("description") if isinstance(body, dict) else ""
        raise TelegramError(description or "Telegram отклонил запрос")
    return body.get("result")


def send_message(token: str, chat_id: str, text: str) -> None:
    _call(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
    })


def bot_name(token: str) -> str:
    me = _call(token, "getMe", timeout=5)
    return f"@{me.get('username')}" if isinstance(me, dict) else ""


def find_chats(token: str) -> list[dict]:
    """Чаты, где бот недавно видел сообщения: так узнаётся id группы.

    По ссылке-приглашению Bot API id группы не отдаёт, поэтому порядок такой:
    добавить бота в группу (админом - иначе в группе он видит только команды),
    написать там /start и нажать «Найти группу». Telegram хранит такие
    обновления около суток.
    """
    updates = _call(token, "getUpdates", {"allowed_updates": '["message","my_chat_member"]'})
    chats: dict[str, dict] = {}
    for update in updates if isinstance(updates, list) else []:
        for key in ("message", "my_chat_member", "channel_post"):
            chat = (update.get(key) or {}).get("chat")
            if chat and "id" in chat:
                title = chat.get("title") or " ".join(
                    filter(None, [chat.get("first_name"), chat.get("last_name")])) or "личный чат"
                chats[str(chat["id"])] = {"id": str(chat["id"]), "title": title,
                                          "type": chat.get("type", "")}
    return list(chats.values())
