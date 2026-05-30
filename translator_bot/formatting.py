from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import escape


MAX_TELEGRAM_MESSAGE_LENGTH = 3900
PROTECTED_TOKEN_PREFIX = "XM_PROTECTED_SEGMENT_"


@dataclass(frozen=True)
class ProtectedSegment:
    kind: str
    raw: str
    content: str
    language: str | None = None


@dataclass(frozen=True)
class ProtectedRange:
    kind: str
    start: int
    end: int
    language: str | None = None


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
    alerts: dict[str, bool] | None = None,
) -> str:
    sender = f"<code>{escape(sender_name)}</code>"
    if sender_username:
        sender = f"{sender} (@{escape(sender_username)})"

    chat = _format_chat_name(chat_title, chat_title_translation)
    parts = [
        f"{_importance_emoji(important, alerts)}{chat}",
        f"👤 {sender}",
    ]
    if include_original:
        parts.extend(["", "🇨🇳", render_message_body(original)])
    parts.extend(["", "🇬🇧", render_message_body(translation)])
    return "\n".join(part for part in parts if part != "")


def format_summary(
    title: str,
    body: str,
    generated_at: datetime,
    chat_name: str | None = None,
    chat_name_translation: str | None = None,
    timezone_name: str = "UTC",
) -> str:
    header = f"📝 <b>{escape(title)}</b>"
    if chat_name:
        header = f"{header} {_format_chat_name(chat_name, chat_name_translation)}"
    return f"{header}\n🕒 {generated_at:%Y-%m-%d %H:%M} {escape(timezone_name)}\n\n{escape(body)}"


def _format_chat_name(chat_name: str, chat_name_translation: str | None) -> str:
    original = f"<code>{escape(chat_name)}</code>"
    if not chat_name_translation or chat_name_translation.casefold() == chat_name.casefold():
        return original
    return f"{original} / <code>{escape(chat_name_translation)}</code>"


def _importance_emoji(important: bool, alerts: dict[str, bool] | None) -> str:
    if alerts:
        if alerts.get("urgent"):
            return "🚨 "
        if alerts.get("name_mention"):
            return "📢 "
        if alerts.get("question_or_request"):
            return "❓ "
    if important:
        return "⚠️ "
    return ""


def render_message_body(text: str) -> str:
    """Render plain text while preserving newlines and common Markdown code fences."""
    parts: list[str] = []
    position = 0
    for match in re.finditer(r"```([^\n`]*)\n?(.*?)```", text, flags=re.DOTALL):
        parts.append(_render_inline_code(text[position : match.start()]))
        language = match.group(1).strip()
        code = match.group(2).rstrip("\n")
        if not code and language:
            parts.append(f"<code>{escape(language)}</code>")
        else:
            parts.append(f"<pre>{escape(code)}</pre>")
        position = match.end()
    parts.append(_render_inline_code(text[position:]))
    return "".join(parts)


def _render_inline_code(text: str) -> str:
    escaped = escape(text)
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)


def mask_protected_segments(text: str) -> tuple[str, dict[str, ProtectedSegment]]:
    protected: dict[str, ProtectedSegment] = {}
    masked_parts: list[str] = []
    position = 0

    for match in re.finditer(r"```([^\n`]*)\n?(.*?)```", text, flags=re.DOTALL):
        masked_parts.append(_mask_inline_code(text[position : match.start()], protected))
        language = match.group(1).strip()
        code = match.group(2)
        kind = "inline_code" if language and not code else "code_block"
        raw = f"`{language}`" if kind == "inline_code" else match.group(0)
        token = _next_protected_token(protected)
        protected[token] = ProtectedSegment(
            kind=kind,
            raw=raw,
            content=language if kind == "inline_code" else code,
            language=language or None,
        )
        masked_parts.append(token)
        position = match.end()

    masked_parts.append(_mask_inline_code(text[position:], protected))
    return "".join(masked_parts), protected


def mask_protected_ranges(text: str, ranges: list[ProtectedRange]) -> tuple[str, str, dict[str, ProtectedSegment]]:
    protected: dict[str, ProtectedSegment] = {}
    masked_parts: list[str] = []
    rendered_parts: list[str] = []
    position = 0

    for protected_range in sorted(ranges, key=lambda item: item.start):
        if protected_range.start < position:
            continue
        _append_masked_text(text[position : protected_range.start], masked_parts, rendered_parts, protected)

        content = text[protected_range.start : protected_range.end]
        if protected_range.kind == "inline_code" and not content.strip() and protected_range.language:
            content = protected_range.language
        token = _next_protected_token(protected)
        raw = _raw_protected_segment(protected_range.kind, content, protected_range.language)
        protected[token] = ProtectedSegment(
            kind=protected_range.kind,
            raw=raw,
            content=content,
            language=protected_range.language,
        )
        masked_parts.append(token)
        rendered_parts.append(raw)
        position = protected_range.end

    _append_masked_text(text[position:], masked_parts, rendered_parts, protected)

    return "".join(masked_parts), "".join(rendered_parts), protected


def restore_protected_segments(
    text: str,
    protected: dict[str, ProtectedSegment],
    *,
    append_missing: bool = False,
) -> str:
    restored = text
    for token, segment in protected.items():
        if token in restored:
            restored = restored.replace(token, segment.raw)
        elif append_missing:
            restored = f"{restored.rstrip()}\n{segment.raw}"
    return restored


def protected_segments_for_ai(protected: dict[str, ProtectedSegment]) -> list[dict[str, str | None]]:
    segments: list[dict[str, str | None]] = []
    for token, segment in protected.items():
        segments.append(
            {
                "placeholder": token,
                "type": segment.kind,
                "language": segment.language,
                "content": segment.content if segment.kind == "inline_code" else None,
            }
        )
    return segments


def _mask_inline_code(text: str, protected: dict[str, ProtectedSegment]) -> str:
    def replace(match: re.Match[str]) -> str:
        token = _next_protected_token(protected)
        protected[token] = ProtectedSegment(
            kind="inline_code",
            raw=match.group(0),
            content=match.group(1),
        )
        return token

    return re.sub(r"`([^`\n]+)`", replace, text)


def _next_protected_token(protected: dict[str, ProtectedSegment]) -> str:
    return f"{PROTECTED_TOKEN_PREFIX}{len(protected)}"


def _append_masked_text(
    text: str,
    masked_parts: list[str],
    rendered_parts: list[str],
    protected: dict[str, ProtectedSegment],
) -> None:
    masked_text, rendered_text, local_segments = _mask_markdown_segments_with_rendered_text(text)
    for old_token, segment in local_segments.items():
        new_token = _next_protected_token(protected)
        protected[new_token] = segment
        masked_text = masked_text.replace(old_token, new_token)
    masked_parts.append(masked_text)
    rendered_parts.append(rendered_text)


def _mask_markdown_segments_with_rendered_text(text: str) -> tuple[str, str, dict[str, ProtectedSegment]]:
    masked_text, protected = mask_protected_segments(text)
    return masked_text, restore_protected_segments(masked_text, protected), protected


def _raw_protected_segment(kind: str, content: str, language: str | None) -> str:
    if kind == "code_block":
        language_text = language or ""
        trailing_newline = "" if content.endswith("\n") else "\n"
        return f"```{language_text}\n{content}{trailing_newline}```"
    return f"`{content}`"
