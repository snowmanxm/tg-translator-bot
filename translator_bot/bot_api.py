from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
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


def send_bot_media_sync(
    bot_token: str,
    chat_id: int | str,
    file_path: str | Path,
    *,
    media_type: str,
    caption: str | None = None,
    parse_mode: str | None = None,
) -> None:
    method, field_name = _media_method_and_field(media_type)
    data: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if parse_mode:
        data["parse_mode"] = parse_mode
    _bot_api_request_multipart(bot_token, method, data=data, file_field=field_name, file_path=Path(file_path))


def set_bot_commands_sync(bot_token: str, commands: list[dict[str, str]]) -> None:
    _bot_api_request(bot_token, "setMyCommands", data={"commands": json.dumps(commands)})


async def send_bot_message(bot_token: str, chat_id: int | str, text: str, *, parse_mode: str | None = None) -> None:
    import asyncio

    await asyncio.to_thread(send_bot_message_sync, bot_token, chat_id, text, parse_mode=parse_mode)


async def send_bot_media(
    bot_token: str,
    chat_id: int | str,
    file_path: str | Path,
    *,
    media_type: str,
    caption: str | None = None,
    parse_mode: str | None = None,
) -> None:
    import asyncio

    await asyncio.to_thread(
        send_bot_media_sync,
        bot_token,
        chat_id,
        file_path,
        media_type=media_type,
        caption=caption,
        parse_mode=parse_mode,
    )


async def set_bot_commands(bot_token: str, commands: list[dict[str, str]]) -> None:
    import asyncio

    await asyncio.to_thread(set_bot_commands_sync, bot_token, commands)


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


def _bot_api_request_multipart(
    bot_token: str,
    method: str,
    *,
    data: dict[str, Any],
    file_field: str,
    file_path: Path,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    boundary = f"----xmtranslator{uuid.uuid4().hex}"
    body = bytearray()

    for key, value in data.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    filename = file_path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    request = Request(
        url,
        data=bytes(body),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise BotApiError(f"Telegram Bot API returned HTTP {exc.code}: {body_text}") from exc
    except URLError as exc:
        raise BotApiError(f"Could not reach Telegram Bot API: {exc.reason}") from exc

    if not payload.get("ok"):
        description = payload.get("description", "unknown error")
        raise BotApiError(f"Telegram Bot API error: {description}")
    return payload


def _media_method_and_field(media_type: str) -> tuple[str, str]:
    mapping = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "animation": ("sendAnimation", "animation"),
        "audio": ("sendAudio", "audio"),
        "voice": ("sendVoice", "voice"),
        "video_note": ("sendVideoNote", "video_note"),
        "sticker": ("sendSticker", "sticker"),
        "document": ("sendDocument", "document"),
    }
    return mapping.get(media_type, ("sendDocument", "document"))
