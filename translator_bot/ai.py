from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from translator_bot.config import Settings


@dataclass(frozen=True)
class TranslationResult:
    english: str
    important: bool
    reason: str | None = None


class OpenAIService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai.api_key)

    async def translate(self, text: str, *, chat_title: str, sender_name: str) -> TranslationResult:
        response = await self.client.chat.completions.create(
            model=self.settings.openai.translation_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate Chinese chat messages to natural English. "
                        "Return JSON with keys: english, important, reason. "
                        "Mark important true for requests, decisions, deadlines, complaints, "
                        "money/payment, meetings, risks, or urgent messages. "
                        "For unimportant messages, reason must be null. "
                        "For important messages, reason must be under 80 characters."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"chat": chat_title, "sender": sender_name, "message": text},
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        data = _json_from_response(response.choices[0].message.content)
        important = bool(data.get("important", False))
        reason = str(data["reason"]).strip() if important and data.get("reason") else None
        return TranslationResult(
            english=str(data.get("english", "")).strip() or text,
            important=important,
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

    async def analyze_alerts(self, text: str, *, sender_name: str, chat_title: str) -> dict[str, Any]:
        names = self.settings.alerts.names
        response = await self.client.chat.completions.create(
            model=self.settings.openai.alert_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Analyze a Telegram message. Return JSON with booleans: "
                        "name_mention, question_or_request, urgent. "
                        "Do not include explanations or reason fields. "
                        "Only set true when the signal is clear."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "watched_names": names,
                            "chat": chat_title,
                            "sender": sender_name,
                            "message": text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        data = _json_from_response(response.choices[0].message.content)
        return {
            "name_mention": bool(data.get("name_mention")) if self.settings.alerts.name_mentions_enabled else False,
            "question_or_request": bool(data.get("question_or_request"))
            if self.settings.alerts.question_request_alert
            else False,
            "urgent": bool(data.get("urgent")) if self.settings.alerts.urgency_alert else False,
        }

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
                        "Summarize Telegram chat messages in concise English bullets. Include: "
                        "1) key points, 2) decisions, 3) action items, 4) questions/requests, "
                        "5) meetings/events/deadlines, 6) urgent or risky items. "
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
