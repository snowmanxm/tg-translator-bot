from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


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
    sender_name: str,
    original: str,
    translation: str,
    include_original: bool,
    alerts: dict[str, Any] | None = None,
) -> str:
    alert_lines = []
    if alerts:
        if alerts.get("name_mention"):
            alert_lines.append("Name mention")
        if alerts.get("question_or_request"):
            alert_lines.append("Question/request")
        if alerts.get("urgent"):
            alert_lines.append("Urgent")
    parts = [
        f"Chat: {escape(chat_title)}",
        f"From: {escape(sender_name)}",
    ]
    if alert_lines:
        parts.append(f"Alerts: {', '.join(alert_lines)}")
    if include_original:
        parts.extend(["", escape(original)])
    parts.extend(["", escape(translation)])
    return "\n".join(part for part in parts if part != "")


def format_summary(title: str, body: str, generated_at: datetime) -> str:
    return f"<b>{escape(title)}</b>\nGenerated: {generated_at:%Y-%m-%d %H:%M UTC}\n\n{escape(body)}"
