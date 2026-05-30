from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BotApiError(RuntimeError):
    pass


def fetch_bot_updates(bot_token: str) -> list[dict[str, Any]]:
    payload = _bot_api_request(bot_token, "getUpdates")
    result = payload.get("result", [])
    return result if isinstance(result, list) else []


def send_bot_message_sync(bot_token: str, chat_id: int | str, text: str, *, parse_mode: str | None = None) -> None:
    data: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    _bot_api_request(bot_token, "sendMessage", data=data)


async def send_bot_message(bot_token: str, chat_id: int | str, text: str, *, parse_mode: str | None = None) -> None:
    import asyncio

    await asyncio.to_thread(send_bot_message_sync, bot_token, chat_id, text, parse_mode=parse_mode)


def _bot_api_request(bot_token: str, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    encoded_data = urlencode(data).encode("utf-8") if data else None
    request = Request(url, data=encoded_data, method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BotApiError(f"Telegram Bot API returned HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise BotApiError(f"Could not reach Telegram Bot API: {exc.reason}") from exc

    if not payload.get("ok"):
        description = payload.get("description", "unknown error")
        raise BotApiError(f"Telegram Bot API error: {description}")
    return payload
