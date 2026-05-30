from __future__ import annotations

from datetime import datetime
from html import escape


MAX_TELEGRAM_MESSAGE_LENGTH = 3900


def split_telegram_message(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if line_len > limit:
            chunks.append(line[:limit])
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def format_translation(
    *,
    chat_title: str,
    chat_title_translation: str | None,
    sender_name: str,
    sender_username: str | None,
    original: str,
    translation: str,
    include_original: bool,
    important: bool = False,
) -> str:
    sender = f"<code>{escape(sender_name)}</code>"
    if sender_username:
        sender = f"{sender} (@{escape(sender_username)})"

    chat = _format_chat_name(chat_title, chat_title_translation)
    parts = [
        f"{'⚠️ ' if important else ''}{chat}",
        f"👤 {sender}",
    ]
    if include_original:
        parts.extend(["", f"🇨🇳 {escape(original)}"])
    parts.extend(["", f"🇬🇧 {escape(translation)}"])
    return "\n".join(part for part in parts if part != "")


def format_summary(
    title: str,
    body: str,
    generated_at: datetime,
    chat_name: str | None = None,
    chat_name_translation: str | None = None,
) -> str:
    header = f"📝 <b>{escape(title)}</b>"
    if chat_name:
        header = f"{header} {_format_chat_name(chat_name, chat_name_translation)}"
    return f"{header}\n🕒 {generated_at:%Y-%m-%d %H:%M UTC}\n\n{escape(body)}"


def _format_chat_name(chat_name: str, chat_name_translation: str | None) -> str:
    original = f"<code>{escape(chat_name)}</code>"
    if not chat_name_translation or chat_name_translation.casefold() == chat_name.casefold():
        return original
    return f"{original} / <code>{escape(chat_name_translation)}</code>"
