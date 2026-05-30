from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from translator_bot.config import Settings


@dataclass(frozen=True)
class MessageAnalysisResult:
    chat_title_english: str
    message_english: str
    important: bool
    alerts: dict[str, bool]
    reason: str | None = None


class OpenAIService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai.api_key)

    async def analyze_message(
        self,
        text: str,
        *,
        chat_title: str,
        sender_name: str,
        protected_placeholders: list[dict[str, str | None]] | None = None,
        known_chat_title_english: str | None = None,
    ) -> MessageAnalysisResult:
        names = self.settings.alerts.names
        response = await self.client.chat.completions.create(
            model=self.settings.openai.translation_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Analyze one Chinese Telegram message. Return JSON only with keys: "
                        "chat_title_english, message_english, important, important_reason, alerts. "
                        "alerts must be an object with booleans: name_mention, question_or_request, urgent. "
                        "Translate the message into natural English. Translate the chat title into concise English; "
                        "if known_chat_title_english is provided, reuse it unless clearly wrong. "
                        "The message may contain protected placeholders like XM_PROTECTED_SEGMENT_0. "
                        "Copy placeholders exactly into message_english where they belong. "
                        "Never translate, remove, duplicate, wrap, indent, or modify placeholders. "
                        "Use inline_code placeholder content as context only; do not translate that content. "
                        "Code block placeholder content is intentionally omitted and must not be inferred. "
                        "Mark important true for requests, questions, decisions, deadlines, complaints, "
                        "money/payment, meetings, risks, urgent messages, or watched-name mentions. "
                        "For unimportant messages, important_reason must be null. "
                        "For important messages, important_reason must be under 80 characters. "
                        "Do not include explanations."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "watched_names": names,
                            "chat_title": chat_title,
                            "known_chat_title_english": known_chat_title_english,
                            "sender": sender_name,
                            "message": text,
                            "protected_placeholders": protected_placeholders or [],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        data = _json_from_response(response.choices[0].message.content)
        alerts = data.get("alerts") if isinstance(data.get("alerts"), dict) else {}
        compact_alerts = {
            "name_mention": bool(alerts.get("name_mention")) if self.settings.alerts.name_mentions_enabled else False,
            "question_or_request": bool(alerts.get("question_or_request"))
            if self.settings.alerts.question_request_alert
            else False,
            "urgent": bool(alerts.get("urgent")) if self.settings.alerts.urgency_alert else False,
        }
        important = (
            bool(data.get("important"))
            or compact_alerts["name_mention"]
            or compact_alerts["question_or_request"]
            or compact_alerts["urgent"]
        )
        reason = str(data["important_reason"]).strip() if important and data.get("important_reason") else None
        return MessageAnalysisResult(
            chat_title_english=str(data.get("chat_title_english", "")).strip()
            or known_chat_title_english
            or chat_title,
            message_english=str(data.get("message_english", "")).strip() or text,
            important=important,
            alerts=compact_alerts,
            reason=_short_text(reason, 80),
        )

    async def translate_batch(self, messages: list[dict[str, Any]]) -> str:
        response = await self.client.chat.completions.create(
            model=self.settings.openai.translation_model,
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate these Chinese Telegram messages to English. "
                        "Keep each message on its own bullet with sender and short context."
                    ),
                },
                {"role": "user", "content": json.dumps(messages, ensure_ascii=False, default=str)},
            ],
        )
        return response.choices[0].message.content or ""

    async def summarize(self, messages: list[dict[str, Any]], *, title: str) -> str:
        if not messages:
            return "No messages to summarize."

        response = await self.client.chat.completions.create(
            model=self.settings.openai.summary_model,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize Telegram chat messages in concise English. "
                        "Use exactly these emoji section headings when relevant: "
                        "🔑 Key Points, ✅ Decisions, 📌 Action Items, ❓ Questions/Requests, "
                        "📅 Meetings/Events/Deadlines, ⚠️ Urgent or Risky Items. "
                        "Use short bullets under each heading. End every non-empty bullet with "
                        "the source chat display in parentheses, wrapping chat names with backticks. "
                        "Use the provided chat_display field exactly as-is when available, for example: "
                        "'- Backend support can wait until tomorrow. (`技术群` / `Tech Group`)' "
                        "If a section has no useful content, write '- None mentioned.' "
                        "Ignore small talk unless it changes the situation."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"title": title, "messages": messages}, ensure_ascii=False, default=str),
                },
            ],
        )
        return response.choices[0].message.content or "No summary generated."


def _json_from_response(content: str | None) -> dict[str, Any]:
    if not content:
        return {}
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _short_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."
